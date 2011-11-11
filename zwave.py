#!/usr/bin/env python 
# -- coding: utf-8 --
import openzwave, time, json
import datetime
import os
import ConfigParser
from twisted.internet import reactor, defer
from houseagent.plugins import pluginapi
from houseagent import config_path

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
        options = openzwave.PyOptions()
        options.create('config/', '' , '') #--logging false
        options.lock()
        
        self.manager = openzwave.PyManager()
        self.manager.create()
        self.manager.addWatcher(self.ozw_callback)
        self.manager.addDriver(self.interface)
        
        callbacks = {'poweron': self.cb_poweron,
                     'poweroff': self.cb_poweroff,
                     'custom': self.cb_custom,
                     'thermostat_setpoint': self.cb_thermostat}
        
        self.pluginapi = pluginapi.PluginAPI(self.id, self.PLUGIN_TYPE, **callbacks)
        
    def get_configurationparameters(self):
        '''
        This function parses configuration parameters from the zwave.conf file.
        Fallback values are assigned in case the configuration file or parameter is not found.
        '''
        config_file = os.path.join(config_path, 'zwave', 'zwave.conf')
        
        config = ConfigParser.RawConfigParser()
        if os.path.exists(config_file):
            config.read(config_file)
        else:
            config.read('zwave.conf')
        
        self.broker_host = config.get('broker', 'host')
        self.broker_port = config.getint('broker', 'port')
        self.broker_user = config.get('broker', 'username')
        self.broker_pass = config.get('broker', 'password')
        self.broker_vhost = config.get('broker', 'vhost')
        
        self.logging = config.get('general', 'loglevel')
        self.interface = config.get('serial', 'port')
        self.id = config.get('general', 'id')
        
        self.report_values = config._sections['report_values']
        
    def cb_poweron(self, node_id):
        '''
        This function is called when a poweron request has been received from the network.
        @param node_id: the node_id to power on.
        '''
        node_id = int(node_id)
        d = defer.Deferred()
        self.manager.setNodeOn(self.home_id, node_id)
        d.callback('done!')
        return d
        
    def cb_poweroff(self, node_id):
        '''
        This function is called when a poweroff request has been received from the network.
        @param node_id: the node_id to power off.
        '''
        node_id = int(node_id)
        d = defer.Deferred()
        self.manager.setNodeOff(self.home_id, node_id)
        d.callback('done!')
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
        This callback function handles custom requests send to the z-wave plugin.
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
            values = {}
            
            node = int(parameters["node"])
            node = self.get_node(self.home_id, node)
                    
            if not isinstance(node, ZwaveNode):
                d = defer.Deferred()
                d.callback('Invalid node specified.')
                return d
                
            for value in node.values:
                try:               
                    valueinfo = {"class": value.value_data["commandClass"],
                                 "label": value.value_data["label"],
                                 "units": value.value_data["units"],
                                 "value": value.value_data["value"] }  
                except:
                    pass              
                
                values[value.value_data["id"]] = valueinfo

            d = defer.Deferred()
            d.callback(values)
            return d
        
        if action == 'track_values':
            node_id = parameters['node']
            values  = parameters['values']
            
            config = ConfigParser.RawConfigParser()
            config.read('zwave.conf')
            
            try:
                value_list = json.loads(config.get("report_values", str(node_id)))
            except: 
                value_list = []
                
            for value in values:
                if value not in value_list:
                    value_list.append(value)
                    
            config.set("report_values", str(node_id), json.dumps(value_list))
            
            # update dictionary
            self.report_values = config._sections['report_values']
            
            with open('zwave.conf', 'wb') as configfile:
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
        This function handles callbacks from the open-zwave interface.
        @param args: a callback dict
        '''
        type = args['notificationType']
        if type == 'NodeAdded':
            self.cb_node_added(args)
        elif type == 'ValueAdded':
            self.cb_value_added(args)
        elif type == 'ValueChanged':
            self.cb_value_changed(args)
        elif type in ('AwakeNodesQueried', 'AllNodesQueried'):
            self.cb_all_nodes_queried(args)
                        
    def cb_node_added(self, args):
        '''
        This callback function is called when a node has been discovered or added. 
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
            if self.report_values.has_key(str(node_id)):          
                if str(value_id) in self.report_values[str(node_id)]:
                    report_values = {}
                    report_values[value.value_data["label"]] = value.value_data["value"]
                    self.pluginapi.value_update(str(node.node_id), report_values)
        
    def cb_all_nodes_queried(self, args):
        '''
        This callback function is called when the open-zwave interface is ready,
        when all nodes have been queried.
        We utilize this to query some more node information.
        @param args: a dict containing the relevant callback data.
        '''
        self.home_id = args['homeId']
        
        for node in self.nodes:
            node.last_update = time.time()
            node.manufacturer = self.manager.getNodeManufacturerName(node.home_id, node.node_id)
            node.product = self.manager.getNodeProductName(node.home_id, node.node_id)
            node.listening = self.manager.isNodeListeningDevice(node.home_id, node.node_id)
            
        self.manager.writeConfig(self.home_id)
        self.pluginapi.ready()
        
if os.name == 'nt':        
    class ZwaveService(pluginapi.WindowsService):
        '''
        This class provides a Windows Service interface for the z-wave plugin.
        '''
        _svc_name_ = "hazwave"
        _svc_display_name_ = "HouseAgent - Zwave Service"
        
        def start(self):
            '''
            Start the Zwave interface.
            '''
            ZwaveWrapper()
        
if __name__ == '__main__':
    if os.name == 'nt':
        pluginapi.handle_windowsservice(ZwaveService) # We want to start as a Windows service on Windows.
    else:
        ZwaveWrapper()
        reactor.run()