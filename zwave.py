# -*- coding: utf-8 -*-
import openzwave
import ConfigParser
from plugins.pluginapi import PluginAPI
from twisted.python import log
from twisted.internet import reactor
from utils.generic import get_configurationpath
import win32service
import win32serviceutil
import win32event
import win32evtlogutil
import os, sys
from openzwave import PyManager
from collections import namedtuple
import time, json, datetime
from louie import dispatcher, All

NamedPair = namedtuple('NamedPair', ['id', 'name'])
NodeInfo = namedtuple('NodeInfo', ['generic','basic','specific','security','version'])
GroupInfo = namedtuple('GroupInfo', ['index','label','maxAssociations','members'])

class ZWaveWrapperException(Exception):
    '''Exception class for ZWave Wrapper'''
    def __init__(self, value):
        Exception.__init__(self)
        self.value = value

    def __str__(self):
        return repr(self.value)

class ZWaveValueNode:
    '''
    Skeleton class for a single value for an OZW node element.
    '''
    def __init__(self, home_id, node_id, value_data):
        '''
        Initialize value node
        @param home_id: ID of home/driver
        @param node_id: ID of node
        @param value_data: valueId dict (see openzwave.pyx)
        '''
        self.home_id = home_id
        self.node_id = node_id
        self.value_data = value_data
        self.last_update = None

    def get_value(self, key):
        return self.value_data[key] if self.value_data.has_key(key) else None
    
    def update(self, args):
        '''Update node value from callback arguments'''
        self.value_data = args['valueId']
        self.last_update = time.time()

    def __str__(self):
        return 'home_id: [{0}]  node_id: [{1}]  value_data: {2}'.format(self.home_id, self.node_id, self.value_data)

class ZWaveNode:
    '''
    Represents a single device within the Z-Wave Network
    '''

    def __init__(self, home_id, node_id):
        '''
        Initialize zwave node
        @param home_id: ID of home/driver
        @param node_id: ID of node
        '''
        self.last_update = None
        self.home_id = home_id
        self.node_id = node_id
        self._capabilities = set()
        self.command_classes = set()
        self.neighbors = set()
        self.values = dict()
        self.name = ''
        self.location = ''
        self._manufacturer = None
        self._product = None
        self._product_type = None
        self.groups = list()
        self.sleeping = True

    is_locked = property(lambda self: self._get_is_locked())
    level = property(lambda self: self._get_level())
    is_on = property(lambda self: self._get_is_on())
    battery_level = property(lambda self: self._get_battery_level())
    signal_strength = property(lambda self: self._get_signal_strength())
    capabilities = property(lambda self: ', '.join(self._capabilities))
    manufacturer = property(lambda self: self._manufacturer.name if self._manufacturer else '')
    product = property(lambda self: self._product.name if self._product else '')
    product_type = property(lambda self: self._product_type.name if self._product_type else '')

    def _get_is_locked(self):
        return False

    def _get_values_for_command_class(self, class_id):
        retval = list()
        class_str = PyManager.COMMAND_CLASS_DESC[class_id]
        for value in self.values.itervalues():
            vdic = value.value_data
            if vdic and vdic.has_key('commandClass') and vdic['commandClass'] == class_str:
                retval.append(value)
        return retval

    def _get_level(self):
        values = self._get_values_for_command_class(0x26)  # COMMAND_CLASS_SWITCH_MULTILEVEL
        if values:
            for value in values:
                vdic = value.value_data
                if vdic and vdic.has_key('type') and vdic['type'] == 'Byte' and vdic.has_key('value'):
                    return int(vdic['value'])
        return 0

    def _get_battery_level(self):
        values = self._get_values_for_command_class(0x80)  # COMMAND_CLASS_BATTERY
        if values:
            for value in values:
                vdic = value.value_data
                if vdic and vdic.has_key('type') and vdic['type'] == 'Byte' and vdic.has_key('value'):
                    return int(vdic['value'])
        return -1

    def _get_signal_strength(self):
        return 0

    def _get_is_on(self):
        values = self._get_values_for_command_class(0x25)  # COMMAND_CLASS_SWITCH_BINARY
        if values:
            for value in values:
                vdic = value.value_data
                if vdic and vdic.has_key('type') and vdic['type'] == 'Bool' and vdic.has_key('value'):
                    return vdic['value'] == 'True'
        return False

    def has_command_class(self, command_class):
        return command_class in self._commandClasses

class ZWaveWrapper():
    '''
        The purpose of this wrapper is to eliminate some of the tedium of working with
        the underlying API, which is extremely fine-grained.

        Wrapper provides a single, cohesive set of python objects representing the
        current state of the underlying ZWave network.  It is kept in sync with OZW and
        the network via callback hooks.

        Note: This version only handles a single Driver/Controller.  Modifications will
        be required in order to support more complex ZWave deployments.
    '''
    SIGNAL_DRIVER_READY = 'driverReady'
    SIGNAL_NODE_ADDED = 'nodeAdded'
    SIGNAL_NODE_READY = 'nodeReady'
    SIGNAL_SYSTEM_READY = 'systemReady'
    SIGNAL_VALUE_CHANGED = 'valueChanged'

    ignoreSubsequent = True

    def __init__(self, configset):
        self._initialized = False
        self._home_id = None
        self._controller_node_id = None
        self._controller = None
        self._nodes = dict()
        self._library_type_name = 'Unknown'
        self._library_version = 'Unknown'        

        from utils.generic import get_configurationpath
        config_path = get_configurationpath()
        
        config = ConfigParser.RawConfigParser()
        config.read(os.path.join(config_path, 'zwave', 'zwave.conf'))
        
        # Get broker information (RabbitMQ)
        self.broker_host = config.get("broker", "host")
        self.broker_port = config.getint("broker", "port")
        self.broker_user = config.get("broker", "username")
        self.broker_pass = config.get("broker", "password")
        self.broker_vhost = config.get("broker", "vhost")
        
        # Other configuration items
        self.logging = config.getboolean('general', 'logging')
        self._device = config.get("serial", "port")
        self.id = config.get('general', 'id')
        
        self.pluginapi = PluginAPI(plugin_id=self.id, plugin_type='Zwave', logging=self.logging,
                                   broker_ip=self.broker_host, broker_port=self.broker_port,
                                   username=self.broker_user, password=self.broker_pass, vhost=self.broker_vhost)
        
        self.pluginapi.register_custom(self)
        self.pluginapi.register_poweron(self)
        self.pluginapi.register_poweroff(self)
        self.pluginapi.register_thermostat_setpoint(self)

        try:
            value_list = json.loads(config.get("general", "report_values"))
        except: 
            value_list = []
            
        self._report_values = value_list
        
        options = openzwave.PyOptions()
        if os.name == 'nt':
            user_config_path = os.path.join(get_configurationpath(), 'zwave') + "\\"
        else:
            user_config_path = ''
            
        options.create(configset, user_config_path , '') # --logging false
        options.lock()
        self._manager = openzwave.PyManager()
        self._manager.create()
        self._manager.addWatcher(self.zwcallback)
        self._manager.addDriver(self._device)

    controller_description = property(lambda self: self._get_control())
    node_count = property(lambda self: len(self._nodes))
    node_count_description = property(lambda self: self._get_node_count_description())
    sleeping_node_count = property(lambda self: self._get_sleeping_node_count())
    library_description = property(lambda self: self._get_library_description())

    def on_poweron(self, address):
        self._set_node_on(int(address))
        return {'processed': True} 

    def on_poweroff(self, address):
        self._set_node_off(int(address))
        return {'processed': True}
    
    def on_thermostat_setpoint(self, address, setpoint):
        '''
        Called when a thermostat setpoint request has been received from the coordinator.
        '''
        for node in self._nodes:
            for v in self._nodes[node].values:
                if self._nodes[node].values[v].value_data["commandClass"] == "COMMAND_CLASS_THERMOSTAT_SETPOINT":
                    value_id = int(self._nodes[node].values[v].value_data["id"])
                    self._manager.setValue(value_id, float(setpoint))
        return {'processed': True}           

    def on_custom(self, command, parameters):
        """
        Handles several custom actions used througout the plugin.
        """
        if command == 'get_networkinfo':
            nodes = {}
            
            for node in self._nodes:
                if not node == self._controller_node_id:
                    nodeinfo = {"manufacturer": self._nodes[node].manufacturer,
                                "product": self._nodes[node].product,
                                "producttype": self._nodes[node].product_type,
                                "capabilities": self._nodes[node].capabilities,
                                "sleeping": self._nodes[node].sleeping,
                                "lastupdate": datetime.datetime.fromtimestamp(self._nodes[node].lastupdate).strftime("%d-%m-%Y %H:%M:%S")}
                
                    nodes[node] = nodeinfo
            
            return nodes
        elif command == "get_nodevalues":
            values = {}
            
            node = int(parameters["node"])
            for value in self._nodes[node].values:               
                valueinfo = {"class": self._nodes[node].values[value].value_data["commandClass"],
                             "label": self._nodes[node].values[value].value_data["label"],
                             "units": self._nodes[node].values[value].value_data["units"],
                             "value": self._nodes[node].values[value].value_data["value"] }                
                
                values[self._nodes[node].values[value].value_data["id"]] = valueinfo
                
            return values
        elif command == "track_values":
            '''
            With this command you can set certain z-wave values to be tracked, and sent to the master node.
            '''
            config = ConfigParser.RawConfigParser()
            config.read('zwave.conf')

            try:
                value_list = json.loads(config.get("general", "report_values"))
            except: 
                value_list = []
            
            for value in parameters["values"]:
                if value not in value_list:
                    value_list.append(value)
                        
            config.set("general", "report_values", json.dumps(value_list))

            with open('zwave.conf', 'wb') as configfile:
                config.write(configfile)
                
            self._report_values = value_list               

        elif command == "power_on":
            node = parameters["address"]
            self._set_node_on(node)
            return {'processed': True}
        elif command == "power_off":
            node = parameters["address"]
            self.set_node_off(node)
            return {'processed': True}    

    def _get_sleeping_node_count(self):
        retval = 0
        for node in self._nodes.itervalues():
            if node.sleeping:
                retval += 1
        return retval - 1 if retval > 0 else 0

    def _get_library_description(self):
        if self._library_type_name and self._library_version:
            return '{0} Library Version {1}'.format(self._library_type_name, self._library_version)
        else:
            return 'Unknown'

    def _get_node_count_description(self):
        retval = '{0} Nodes'.format(self.nodeCount)
        sleep_count = self.sleeping_node_count
        if sleep_count:
            retval = '{0} ({1} sleeping)'.format(retval, sleep_count)
        return retval

    def _get_controller_description(self):
        if self._controller_node_id:
            node = self._get_node(self._home_id, self._controller_node_id)
            if node and node._product:
                return node._product.name
        return 'Unknown Controller'
    
    def zwcallback(self, args):
        '''
        Callback Handler

        @param args: callback dict
        '''
        notif_type = args['notificationType']
        if notif_type == 'DriverReady':
            self._handle_driver_ready(args)
        elif notif_type in ('NodeAdded', 'NodeNew'):
            self._handle_node_changed(args)
        elif notif_type == 'ValueAdded':
            self._handle_value_added(args)
        elif notif_type == 'ValueChanged':
            self._handle_value_changed(args)
        elif notif_type == 'NodeQueriesComplete':
            self._handle_node_query_complete(args)
        elif notif_type in ('AwakeNodesQueried', 'AllNodesQueried'):
            self._handle_initialization_complete(args)
        else:
            log.msg('Skipping unhandled notification type [%s]', notif_type)

        # TODO: handle event
        # TODO: handle group change
        # TODO: handle value removed
        # TODO: handle node removed
        # TODO: handle config params

    def _handle_driver_ready(self, args):
        '''
        Called once OZW has queried capabilities and determined startup values.  home_id
        and node_id of controller are known at this point.
        '''
        self._home_id = args['homeId']
        self._controller_node_id = args['nodeId']
        self._controller = self._fetch_node(self._home_id, self._controller_node_id)
        self._library_version = self._manager.getLibraryVersion(self._home_id)
        self._library_type_name = self._manager.getLibraryTypeName(self._home_id)
        log.msg('Driver ready.  homeId is 0x%0.8x, controller node id is %d, using %s library version %s', self._home_id, self._controller_node_id, self._library_type_name, self._library_version)
        log.msg('OpenZWave Initialization Begins.')
        log.msg('The initialization process could take several minutes.  Please be patient.')
        dispatcher.send(self.SIGNAL_DRIVER_READY, **{'homeId': self._home_id, 'nodeId': self._controller_node_id})

    def _handle_node_query_complete(self, args):
        node = self._get_node(self._home_id, args['nodeId'])
        self._update_node_capabilities(node)
        self._update_node_command_classes(node)
        self._update_node_neighbors(node)
        self._update_node_info(node)
        self._update_node_groups(node)
        log.msg('Z-Wave Device Node {0} is ready.'.format(node.node_id))
        dispatcher.send(self.SIGNAL_NODE_READY, **{'homeId': self._home_id, 'nodeId': args['nodeId']})

    def _get_node(self, home_id, node_id):
        return self._nodes[node_id] if self._nodes.has_key(node_id) else None

    def _fetch_node(self, home_id, node_id):
        '''
        Build a new node and store it in nodes dict
        '''
        retval = self._get_node(home_id, node_id)
        if retval is None:
            retval = ZWaveNode(home_id, node_id)
            log.msg('Created new node with homeId 0x%0.8x, nodeId %d', home_id, node_id)
            self._nodes[node_id] = retval
        return retval

    def _handle_node_changed(self, args):
        node = self._fetch_node(args['homeId'], args['nodeId'])
        node.lastupdate = time.time()
        dispatcher.send(self.SIGNAL_NODE_ADDED, **{'homeId': args['homeId'], 'nodeId': args['nodeId']})

    def _get_value_node(self, home_id, node_id, value_id):
        node = self._get_node(home_id, node_id)
        if node is None:
            raise ZWaveWrapperException('Value notification received before node creation (homeId %.8x, nodeId %d)' % (home_id, node_id))
        vid = value_id['id']
        if node.values.has_key(vid):
            retval = node.values[vid]
        else:
            retval = ZWaveValueNode(home_id, node_id, value_id)
            log.msg('Created new value node with homeId %0.8x, nodeId %d, valueId %s', home_id, node_id, value_id)
            node.values[vid] = retval
        return retval

    def _handle_value_added(self, args):
        home_id = args['homeId']
        controller_node_id = args['nodeId']
        value_id = args['valueId']
        node = self._fetch_node(home_id, controller_node_id)
        node.lastupdate = time.time()
        value_node = self._get_value_node(home_id, controller_node_id, value_id)
        value_node.update(args)
        
        for report_value in self._report_values:
            if int(report_value) == args['valueId']['id']:
                
                values = {args['valueId']['label'] : args['valueId']['value']}
                self.pluginapi.value_update(args['valueId']['nodeId'], values)

    def _handle_value_changed(self, args):
        home_id = args['homeId']
        controller_node_id = args['nodeId']
        value_id = args['valueId']
        node = self._fetch_node(home_id, controller_node_id)
        node.sleeping = False
        node.lastUpdate = time.time()
        value_node = self._get_value_node(home_id, controller_node_id, value_id)
        value_node.update(args)
        if self._initialized:
            dispatcher.send(self.SIGNAL_VALUE_CHANGED, **{'homeId': home_id, 'nodeId': controller_node_id, 'valueId': value_id})
            
        for report_value in self._report_values:
            if int(report_value) == args['valueId']['id']:
                
                values = {args['valueId']['label'] : args['valueId']['value']}
                self.pluginapi.value_update(args['valueId']['nodeId'], values)

    def _update_node_capabilities(self, node):
        '''Update node's capabilities set'''
        nodecaps = set()
        if self._manager.isNodeListeningDevice(node.home_id, node.node_id): nodecaps.add('listening')
        if self._manager.isNodeRoutingDevice(node.home_id, node.node_id): nodecaps.add('routing')

        node._capabilities = nodecaps
        log.msg('Node [%d] capabilities are: %s', node.node_id, node._capabilities)

    def _update_node_command_classes(self, node):
        '''Update node's command classes'''
        class_set = set()
        for cls in PyManager.COMMAND_CLASS_DESC:
            if self._manager.getNodeClassInformation(node.home_id, node.node_id, cls):
                class_set.add(cls)
        node._commandClasses = class_set
        log.msg('Node [%d] command classes are: %s', node.node_id, node.command_classes)
        # TODO: add command classes as string

    def _update_node_neighbors(self, node):
        '''Update node's neighbor list'''
        # TODO: I believe this is an OZW bug, but sleeping nodes report very odd (and long) neighbor lists
        neighborstr = str(self._manager.getNodeNeighbors(node.home_id, node.node_id))
        if neighborstr is None or neighborstr == 'None':
            node.neighbors = None
        else:
            node.neighbors = sorted([int(i) for i in neighborstr.strip('()').split(',')])

        if node.sleeping and node.neighbors is not None and len(node.neighbors) > 10:
            log.msg('Probable OZW bug: Node [%d] is sleeping and reports %d neighbors; marking neighbors as none.', node.node_id, len(node.neighbors))
            node.neighbors = None
            
        log.msg('Node [%d] neighbors are: %s', node.node_id, node.neighbors)

    def _update_node_info(self, node):
        '''Update general node information'''
        node.name = self._manager.getNodeName(node.home_id, node.node_id)
        node.location = self._manager.getNodeLocation(node.home_id, node.node_id)
        node._manufacturer = NamedPair(id=self._manager.getNodeManufacturerId(node.home_id, node.node_id), name=self._manager.getNodeManufacturerName(node.home_id, node.node_id))
        node._product = NamedPair(id=self._manager.getNodeProductId(node.home_id, node.node_id), name=self._manager.getNodeProductName(node.home_id, node.node_id))
        node._product_type = NamedPair(id=self._manager.getNodeProductType(node.home_id, node.node_id), name=self._manager.getNodeType(node.home_id, node.node_id))
        node.nodeInfo = NodeInfo(
            generic = self._manager.getNodeGeneric(node.home_id, node.node_id),
            basic = self._manager.getNodeBasic(node.home_id, node.node_id),
            specific = self._manager.getNodeSpecific(node.home_id, node.node_id),
            security = self._manager.getNodeSecurity(node.home_id, node.node_id),
            version = self._manager.getNodeVersion(node.home_id, node.node_id)
        )

    def _update_node_groups(self, node):
        '''Update node group/association information'''
        groups = list()
        for i in range(0, self._manager.getNumGroups(node.home_id, node.node_id)):
            groups.append(GroupInfo(
                index = i,
                label = self._manager.getGroupLabel(node.home_id, node.node_id, i),
                maxAssociations = self._manager.getMaxAssociations(node.home_id, node.node_id, i),
                members = self._manager.getAssociations(node.home_id, node.node_id, i)
            ))
        node.groups = groups
        log.msg('Node [%d] groups are: %s', node.node_id, node.groups)

    def _update_node_config(self, node):
        log.msg('Requesting config params for node [%d]', node.node_id)
        self._manager.requestAllConfigParams(node.home_id, node.node_id)

    def _handle_initialization_complete(self, args):
        controllercaps = set()
        if self._manager.isPrimaryController(self._home_id): controllercaps.add('primaryController')
        if self._manager.isStaticUpdateController(self._home_id): controllercaps.add('staticUpdateController')
        if self._manager.isBridgeController(self._home_id): controllercaps.add('bridgeController')
        self._controllercaps = controllercaps
        log.msg('Controller capabilities are: %s', controllercaps)
        for node in self._nodes.values():
            self._update_node_capabilities(node)
            self._update_node_command_classes(node)
            self._update_node_neighbors(node)
            self._update_node_info(node)
            self._update_node_groups(node)
            self._update_node_config(node)
        self._initialized = True
        log.msg("OpenZWave initialization is complete.  Found {0} Z-Wave Device Nodes ({1} sleeping)".format(self.node_count, self.sleeping_node_count))
        dispatcher.send(self.SIGNAL_SYSTEM_READY, **{'homeId': self._home_id})
        self._manager.writeConfig(self._home_id)
        # TODO: write config on shutdown as well

    def refresh(self, node):
        log.msg('Requesting refresh for node {0}'.format(node.node_id))
        self._manager.refreshNodeInfo(node.homeId, node.node_id)

    def _set_node_on(self, node):
        self._manager.setNodeOn(self._home_id, node)

    def _set_node_off(self, node):
        self._manager.setNodeOff(self._home_id, node)

    def _set_node_level(self, node, level):
        self._manager.setNodeLevel(self._home_id, node, level)

    def get_command_class_name(self, command_class_code):
        return PyManager.COMMAND_CLASS_DESC[command_class_code]

    def get_command_class_code(self, command_class_name):
        for k, v in PyManager.COMMAND_CLASS_DESC.iteritems():
            if v == command_class_name:
                return k
        return None

class ZwaveService(win32serviceutil.ServiceFramework):
    _svc_name_ = "hazwave"
    _svc_display_name_ = "HouseAgent - Z-wave Service"
    
    def __init__(self,args):
        # Fix the current working directory -- this gets initialized incorrectly
        # for some reason when run as an NT service.
        win32serviceutil.ServiceFramework.__init__(self,args)
        self.hWaitStop=win32event.CreateEvent(None, 0, 0, None)
        self.isAlive=True

    def SvcStop(self):

        # tell Service Manager we are trying to stop (required)
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)

        reactor.stop()
        win32event.SetEvent(self.hWaitStop)
        # set the event to call
        #win32event.SetEvent(self.hWaitStop)

        self.isAlive=False

    def SvcDoRun(self):
        import servicemanager
        
        currentDir = os.path.dirname(sys.executable)
        os.chdir(currentDir)

        # Write a 'started' event to the event log... (not required)
        #
        win32evtlogutil.ReportEvent(self._svc_name_,servicemanager.PYS_SERVICE_STARTED,0,
        servicemanager.EVENTLOG_INFORMATION_TYPE,(self._svc_name_, ''))

        # methode 1: wait for beeing stopped ...
        # win32event.WaitForSingleObject(self.hWaitStop,win32event.INFINITE)

        # methode 2: wait for beeing stopped ...
        self.timeout=1000  # In milliseconds (update every second)

        zwave = Zwave()
       
        if zwave.start():
            win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE) 

        # and write a 'stopped' event to the event log (not required)
        #
        win32evtlogutil.ReportEvent(self._svc_name_,servicemanager.PYS_SERVICE_STOPPED,0,
                                    servicemanager.EVENTLOG_INFORMATION_TYPE,(self._svc_name_, ''))

        self.ReportServiceStatus(win32service.SERVICE_STOPPED)

        return

#if __name__ == '__main__':
#    win32serviceutil.HandleCommandLine(ZwaveService)
wrapper = ZWaveWrapper(configset='config/')
reactor.run()