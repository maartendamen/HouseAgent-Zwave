#!/usr/bin/env python 
# -- coding: utf-8 --

# For development purposes, add the core houseagent codebase also.
import sys
sys.path.append("../HouseAgent")

from houseagent import config_to_location
from houseagent.plugins import pluginapi
from houseagent.utils.error import ConfigFileNotFound
from threading import Event, Thread
from twisted.internet import reactor, defer
import ConfigParser
import datetime
import openzwave
import time
import json
import os

class RepeatTimer(Thread):
    """This class provides functionality to call a function at regular 
    intervals or a specified number of iterations.  The timer can be cancelled
    at any time."""
    
    def __init__(self, interval, function, iterations=0, args=[], kwargs={}):
        Thread.__init__(self)
        self.interval = interval
        self.function = function
        self.iterations = iterations
        self.args = args
        self.kwargs = kwargs
        self.finished = Event()
        self.daemon = True
 
    def run(self):
        count = 0
        while not self.finished.is_set() and (self.iterations <= 0 
                                              or count < self.iterations):
            self.finished.wait(self.interval)
            if not self.finished.is_set():
                self.function(*self.args, **self.kwargs)
                count += 1
 
    def cancel(self):
        self.finished.set()


class ZwaveNode(object):
    '''
    This is a skeleton class represents a z-wave node.
    '''
    def __init__(self, home_id, node_id):
        '''
        Initialize a new ZwaveNode object.
        @param home_id: the home_id of the node
        @param node_id: the node_id of the node
        '''
        self.home_id = home_id
        self.node_id = node_id
        self.last_update = None
        self.manufacturer = None
        self.product = None
        self.listening = True
        self.values = []
    
    def add_value(self, value):
        '''
        Adds a value to this node
        @param value: the value to add to the node
        '''
        self.values.append(value)
        self.fix_labels()
        
    def fix_labels(self):
        '''
        Makes sure this node has no duplicate value labels
        '''
        labels = []
        duplicates = []
        for value in self.values:
            if labels.count(value.value_data["label"]) == 0:
                labels.append(value.value_data["label"])
            elif duplicates.count(value.value_data["label"]) == 0:
                duplicates.append(value.value_data["label"])

        for label in duplicates:
            
            for value in self.values:
                if value.value_data["label"] == label:
                    instance = value.value_data["instance"]
                    index = value.value_data["index"]
                    cclass = value.value_data["commandClass"]
                    break
            
            instanceDifferent = False
            indexDifferent = False
            cclassDifferent = False
            
            for value in self.values:
                if value.value_data["label"] == label:
                    if instance != value.value_data["instance"]:
                        instanceDifferent = True
                    if index != value.value_data["index"]:
                        indexDifferent = True
                    if cclass != value.value_data["commandClass"]:
                        cclassDifferent = True
            
            for value in self.values:
                if value.value_data["label"] == label:
                    value.label = value.value_data["label"] + " "
                    if instanceDifferent:
                        value.label += "#" + str(value.value_data["instance"])
                    if indexDifferent:
                        value.label += "/" + str(value.value_data["index"])
                    if cclassDifferent:
                        value.label += "@" + str(value.value_data["commandClass"]).replace("COMMAND_CLASS_", "")        
        
    def __str__(self):
        return 'home_id: [{0}]  node_id: [{1}]  manufacturer: [{2}] product: [{3}]'.format(self.home_id, 
                                                                                           self.node_id, 
                                                                                           self.manufacturer,                                                                                  self.product)
               
class ZwaveNodeValue(object):
    '''
    This is a skeleton class that represents a z-wave node value.
    '''
    def __init__(self, node, id, value_data):
        '''
        Initialize a new ZwaveNodeValue opbject
        @param node: the node associated with the value
        @param id: the id of the value
        @param value_data: the data of the value
        '''
        self.node = node
        self.id = id
        self.value_data = value_data
        self.label = self.value_data["label"]

    def __str__(self):
        return 'node: {0} label: {1} value_id: {2} value_data: {3}'.format(self.node, self.label, self.id, self.value_data)

class ZwaveWrapper(object):
    '''
    This is the z-wave wrapper class.
    It handles 
    '''
    
    PLUGIN_TYPE = 'Zwave'
    
    def __init__(self):
        '''
        This function initializes the Zwave module.
        '''
        
        self.nodes = []
        self.home_id = None
        
        self.get_configurationparameters()
        
        self.log = pluginapi.Logging("Zwave")
        self.log.set_level(self.loglevel)

        options = openzwave.PyOptions()

        options.create(self.ozw_config_data, '' , 
                       '--logging %s' % self.ozw_logging) 
        options.lock()
        
        self.manager = openzwave.PyManager()
        self.manager.create()
        self.manager.addWatcher(self.ozw_callback)
        self.manager.addDriver(self.ozw_interface)
        
        callbacks = {'poweron': self.cb_poweron,
                     'poweroff': self.cb_poweroff,
                     'custom': self.cb_custom,
                     'thermostat_setpoint': self.cb_thermostat}
        
        # A timer that will call the method 'refresh_nodes' every 
        # ozw_poll_interval seconds.
        self.poll_timer = RepeatTimer(self.ozw_poll_interval, self.refreshNodes)
        
        self.pluginapi = pluginapi.PluginAPI(self.id, self.PLUGIN_TYPE, broker_host='192.168.1.131',
                                             **callbacks)
        
        print self.pluginapi
        
    def refreshNodes(self):
        self.log.debug("refresing nodes")
        for node in range(2, len(self.nodes) + 1):
            self.log.debug("refreshing node %d" % node)
            self.manager.requestNodeState(self.home_id, node)
        
    def get_configurationparameters(self):
        '''
        This function parses configuration parameters from the zwave.conf file.
        Fallback values are assigned in case the configuration file or parameter 
        is not found.
        '''
        
        config = ConfigParser.RawConfigParser()

        # Try and load the plugin config file from the plugin directory under 
        # HouseAgent config directory.
        self.config_file = config_to_location('zwave.conf')
        if not os.path.exists(self.config_file):
            # Maybe we are running development, just load the config file in 
            # current working directory.
            self.config_file = os.path.join(os.getcwd(), 'zwave.conf')
        if not os.path.exists(self.config_file):
            # With no config, we can't continue
            raise ConfigFileNotFound("/etc or working directory.")
        
        # Read in the configuration from the file.
        config.read(self.config_file)
        
        self.loglevel = config.get('general', 'loglevel')
        self.id = config.get('general', 'id')
        
        self.broker_host = config.get('broker', 'host')
        self.broker_port = config.getint('broker', 'port')
        
        self.ozw_interface = config.get('openzwave', 'port')
        self.ozw_config_data = config.get('openzwave', 'config_data')
        self.ozw_poll_interval = config.getint('openzwave', 'poll_interval')
        self.ozw_logging = config.get('openzwave', 'logging')
        
        try:
            report_values = json.loads(config.get("openzwave", "report_values"))
        except: 
            report_values = []
      
        self.report_values = report_values
                
    def cb_poweron(self, node_id):
        '''
        This function is called when a poweron request has been received from 
        the network.
        @param node_id: the node_id to power on.
        '''
        node_id = int(node_id)
        d = defer.Deferred()
        self.manager.setNodeOn(self.home_id, node_id)
        d.callback('done!')
        
        # Need to ensure that we get an up to date status of this node.
        self.log.debug("Requesting state of node %s" % node_id) 
        self.manager.requestNodeState(self.home_id, node_id)

        return d
        
    def cb_poweroff(self, node_id):
        '''
        This function is called when a poweroff request has been received from 
        the network.
        @param node_id: the node_id to power off.
        '''
        node_id = int(node_id)
        d = defer.Deferred()
        self.manager.setNodeOff(self.home_id, node_id)
        d.callback('done!')

        # Need to ensure that we get an up to date status of this node.
        self.log.debug("Requesting state of node %s" % node_id) 
        self.manager.requestNodeState(self.home_id, node_id)
        
        return d
    
    def cb_thermostat(self, node_id, setpoint):
        '''
        This callback function handles setting of thermostat setpoints.
        @param node_id: the node_id of the thermostat
        @param setpoint: the setpoint to set (float)
        '''
        d = defer.Deferred()
        
        node = self.get_node(self.home_id, int(node_id))
        if not isinstance(node, ZwaveNode):
            d.callback('error1') # Specified node not available
        else:
            for val in node.values:
                if val.value_data['commandClass'] == 'COMMAND_CLASS_THERMOSTAT_SETPOINT':
                    value_id = int(val.value_data['id'])
                    self.manager.setValue(value_id, float(setpoint))
                    d.callback('ok')                
        return d
    
    def cb_custom(self, action, parameters):
        '''
        This callback function handles custom requests send to the z-wave 
        plugin.
        @param action: the custom request to handle
        @param parameters: the parameters send with the custom request
        '''
        if action == 'get_networkinfo':
            nodes = {}
            
            for node in self.nodes:
                nodeinfo = {"manufacturer": node.manufacturer,
                            "product": node.product,
                            "lastupdate": datetime.datetime.fromtimestamp(node.last_update).strftime("%d-%m-%Y %H:%M:%S"),
                            'listening': node.listening}
            
                nodes[node.node_id] = nodeinfo
            
            d = defer.Deferred()
            d.callback(nodes)
            return d
        
        if action == 'get_nodevalues': 
            values = []
            
            node = int(parameters["node"])
            node = self.get_node(self.home_id, node)
                    
            if not isinstance(node, ZwaveNode):
                d = defer.Deferred()
                d.callback('Invalid node specified.')
                return d
                
            for value in node.values:
                
                valueinfo = {'id': value.value_data["id"],
                             'class': value.value_data["commandClass"],
                             "label": value.label,
                             "value": "{0} {1}".format(value.value_data["value"], value.value_data["units"]),
                             "track": False
                             }
                
                values.append(valueinfo)

            d = defer.Deferred()
            d.callback(values)
            return d
        
        if action == 'track_value':
            
            value_id = parameters['value_id']
            
            if value_id not in self.report_values:
                self.report_values.append(value_id)
            
            # save config
            config = ConfigParser.RawConfigParser()
            config.read(self.config_file)
            config.set('openzwave', 'report_values', json.dumps(self.report_values))
            with open(self.config_file, 'wb') as configfile:
                config.write(configfile)
        
            # Return something anyway, even though we return nothing.
            d = defer.Deferred()
            d.callback('')
            return d        
        
        if action == 'track_values':
            node_id = parameters['node']
            values  = parameters['values']
            
            config = ConfigParser.RawConfigParser()
            config.read(self.config_file)
            
            try:
                value_list = json.loads(config.get("report_values", 
                                                   str(node_id)))
            except: 
                value_list = []
                
            for value in values:
                if value not in value_list:
                    value_list.append(value)
                    
            config.set("report_values", str(node_id), json.dumps(value_list))
            
            # update dictionary
            self.report_values = config._sections['report_values']
            
            with open(self.config_file, 'wb') as configfile:
                config.write(configfile)
            
            # Update specified values right now
            report_values = {}
            node = self.get_node(self.home_id, node_id)
            for val in node.values:
                if str(val.value_data['id']) in values:
                    report_values[val.value_data["label"]] = val.value_data["value"]
                    
            self.pluginapi.value_update(node_id, report_values)
    
            # Return something anyway, even though we return nothing.
            d = defer.Deferred()
            d.callback('')
            return d
            
    def get_node(self, home_id, node_id):
        '''
        This function gets a node by home_id and node_id.
        @param home_id: the home_id of the node
        @param node_id: the node_id of the node
        '''
        for node in self.nodes:
            if node.home_id == home_id and node.node_id == node_id:
                return node
            
    def get_node_value(self, home_id, node_id, value_id):
        '''
        This function get's a node value object based.
        @param home_id: the home_id of the node
        @param node_id: the node_id of the node
        @param value_id: the value_id of the node value
        '''
        node = self.get_node(home_id, node_id)
        
        for val in node.values:
            if val.id == value_id:
                return val
        
    def ozw_callback(self, args):
        '''
        This function handles callbacks from the open-zwave ozw_interface.
        @param args: a callback dict
        '''
        type = args['notificationType']
        self.log.debug('\n%s\n%s (node %s)\n%s' % ('-' * 30, type, 
                                                   args['nodeId'], '-' * 30))
        if type == 'DriverReady':
            self.cb_driver_ready(args)
        if type == 'NodeAdded':
            self.cb_node_added(args)
        elif type == 'ValueAdded':
            self.cb_value_added(args)
        elif type == 'ValueChanged':
            self.cb_value_changed(args)
        elif type in ('AwakeNodesQueried', 'AllNodesQueried'):
            self.cb_all_nodes_queried(args)
            
    def cb_driver_ready(self, args):
        '''
        Called once OZW has queried capabilities and determined startup values.  
        HomeID and NodeID of controller are known at this point.
        '''
        self.home_id = args['homeId']
                        
    def cb_node_added(self, args):
        '''
        This callback function is called when a node has been discovered or 
        added. 
        @param args: a dict containing the relevant callback data.
        '''
        home_id = args['homeId']
        node_id = args['nodeId']
        node = ZwaveNode(home_id, node_id)
        self.nodes.append(node)
        
    def cb_value_added(self, args):
        '''
        This callback function is called when a node value has been added.
        @param args: a dict containing the relevant callback data.
        '''
        home_id = args['homeId']
        node_id = args['nodeId']

        # In some strange cases a value_added notification is raised
        # without a valueId. Handle this by returning.
        try:
            value_id = args['valueId']['id']
        except KeyError:
            return
        
        node = self.get_node(home_id, node_id)
        node.last_update = time.time()
        
        value = ZwaveNodeValue(node, value_id, args['valueId'])
        node.add_value(value)
        
    def cb_value_changed(self, args):
        '''
        This callback function is called when a node value has been changed.
        @param args: a dict containing the relevant callback data.
        '''
        home_id = args['homeId']
        node_id = args['nodeId']
                
        try:
            value_id = args['valueId']['id']
        except KeyError:
            return
        
        node = self.get_node(home_id, node_id)
        node.last_update = time.time()
        
        value = self.get_node_value(home_id, node_id, value_id)
        if isinstance(value, ZwaveNodeValue):
            value.value_data = args['valueId']

            print "LALALALLA"
            print value_id
            print self.report_values
            for report_value in self.report_values:
                if int(report_value) == value_id:
                    print "INSIDE IF!!!!!!!!!!!!!!"
                    values = {}
                    values[value_id] = value.value_data["value"]
    
                    self.pluginapi.value_update(str(node.node_id), values)                
        
    def cb_all_nodes_queried(self, args):
        '''
        This callback function is called when the open-zwave ozw_interface is 
        ready, when all nodes have been queried.
        We utilize this to query some more node information.
        @param args: a dict containing the relevant callback data.
        '''
        print "all nodes queried!"
        
        self.home_id = args['homeId']
        
        for node in self.nodes:
            node.last_update = time.time()
            node.manufacturer = self.manager.getNodeManufacturerName(node.home_id, 
                                                                     node.node_id)
            node.product = self.manager.getNodeProductName(node.home_id, 
                                                           node.node_id)
            node.listening = self.manager.isNodeListeningDevice(node.home_id, 
                                                                node.node_id)
        
        
        # Write the latest OZW config to file.
        self.manager.writeConfig(self.home_id)
        
        # Start the refresh timer going.
        self.poll_timer.start()

        # Notify HouseAgent that we are ready.
        self.pluginapi.ready()
                


            
if os.name == 'nt':        
    class ZwaveService(pluginapi.WindowsService):
        '''
        This class provides a Windows Service ozw_interface for the z-wave 
        plugin.
        '''
        _svc_name_ = "hazwave"
        _svc_display_name_ = "HouseAgent - Zwave Service"
        
        def start(self):
            '''
            Start the Zwave ozw_interface.
            '''
            ZwaveWrapper()
        
if __name__ == '__main__':
    if os.name == 'nt':
        # We want to start as a Windows service on Windows.
        pluginapi.handle_windowsservice(ZwaveService) 
    else:
        zwave = ZwaveWrapper()
        reactor.run()
