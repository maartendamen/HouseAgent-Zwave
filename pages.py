from mako.lookup import TemplateLookup
from mako.template import Template
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET
from twisted.internet import defer
import json
from twisted.internet.defer import inlineCallbacks

def init_pages(web, coordinator, db):
    web.putChild("zwave_add", Zwave_add(coordinator, db))
    web.putChild("zwave_networkinfo", Zwave_networkinfo(coordinator, db))
    web.putChild("zwave_added", Zwave_added(coordinator, db))
    web.putChild("zwave_values", Zwave_values(coordinator))
    web.putChild('zwave_values_view', Zwave_values_view())
    web.putChild('zwave_track_value', Zwave_track_value(coordinator, db))

"""
Z-wave pages start here
"""    
class Zwave_add(Resource):
    """
    Class that shows an add form to add a z-wave device to the HouseAgent database.
    """
    def __init__(self, coordinator, db):
        Resource.__init__(self)
        self.coordinator = coordinator
        self.db = db
        
    def result(self, result):
        
        lookup = TemplateLookup(directories=['houseagent/templates/'])
        template = Template(filename='houseagent/plugins/zwave/templates/add.html', lookup=lookup)
        
        self.request.write(str(template.render(locations=result, node=self.node, pluginid=self.pluginid, pluginguid=self.pluginguid)))
        self.request.finish()
    
    def render_GET(self, request):
        
        self.request = request    
        self.node = request.args["node"][0]
        self.pluginguid = request.args["pluginguid"][0]
        self.pluginid = request.args["pluginid"][0]
      
        self.db.query_locations().addCallback(self.result)
        
        return NOT_DONE_YET
    
class Zwave_values(Resource):
    
    def __init__(self, coordinator):
        Resource.__init__(self)
        self.coordinator = coordinator
        
    def result(self, result):
        
        self.request.write(json.dumps(result))
        self.request.finish()
        
    def render_GET(self, request):
        
        self.request = request
        self.node = request.args["node"][0]
        self.pluginguid = request.args["pluginguid"][0]
        
        self.coordinator.send_custom(self.pluginguid, "get_nodevalues", {'node': self.node}).addCallback(self.result)
        
        return NOT_DONE_YET
    
class Zwave_values_view(Resource):
        
    def render_GET(self, request):
        self.node = request.args["node"][0]
        self.pluginguid = request.args["pluginguid"][0]
        self.deviceid = request.args["deviceid"][0]
        
        lookup = TemplateLookup(directories=['houseagent/templates/'])
        template = Template(filename='houseagent/plugins/zwave/templates/values.html', lookup=lookup)
        return str(template.render(node=self.node, pluginguid=self.pluginguid, deviceid=self.deviceid))
    
class Zwave_track_value(Resource):
    
    def __init__(self, coordinator, db):
        Resource.__init__(self)
        self.db = db
        self.coordinator = coordinator
    
    def result(self, result):
        self.request.write("done!")
        self.request.finish()
    
    def render_POST(self, request):
        self.pluginguid = request.args["pluginguid"][0]
        self.value_id = request.args['value_id'][0]
        self.label = request.args["label"][0]
        self.deviceid = request.args["deviceid"][0]
        
        deferlist = []
        deferlist.append(self.coordinator.send_custom(self.pluginguid, "track_value", { 'value_id': self.value_id }))
        deferlist.append(self.db.add_value_with_label(self.value_id, self.label, self.deviceid))
        
        return NOT_DONE_YET
    
class Zwave_networkinfo(Resource):
    '''
    This resource display z-wave network information.
    '''
    def __init__(self, coordinator, db):
        Resource.__init__(self)
        self.coordinator = coordinator
        self.db = db
    
    @inlineCallbacks
    def result(self, result):
        
        devices = yield self.db.query_devices()
        
        for node, details in result.iteritems():
            result[node]['in_database'] = 'No'
            for device in devices:
                if node == device[2]:
                    result[node]['deviceid'] = device[0]
                    result[node]['in_database'] = 'Yes'
                    
        
        lookup = TemplateLookup(directories=['houseagent/templates/'])
        template = Template(filename='houseagent/plugins/zwave/templates/networkinfo.html', lookup=lookup)
        
        self.request.write(str(template.render(result=result, pluginguid=self.pluginguid, pluginid=self.pluginid)))
        self.request.finish()

    def query_error(self, error):
        print "FAIL:", error
    
    def render_GET(self, request):
        self.request = request
        plugins = self.coordinator.get_plugins_by_type("Zwave")
        
        if len(plugins) == 0:
            self.request.write(str("No online zwave plugins found..."))
            self.request.finish()
        elif len(plugins) == 1:
            self.pluginguid = plugins[0].guid
            self.pluginid = plugins[0].id
            self.coordinator.send_custom(plugins[0].guid, "get_networkinfo", {}).addCallback(self.result)   
                
        return NOT_DONE_YET       

class Zwave_added(Resource):
    """
    Class that adds a z-wave device to the HouseAgent database.
    """
    def __init__(self, coordinator, db):
        Resource.__init__(self)
        self.coordinator = coordinator      
        self.db = db
        
    def device_added(self, result):       
        self.request.write(str("ok")) 
        self.request.finish()             
    
    def render_POST(self, request):
        self.request = request
        data = json.loads(request.content.read())

        self.db.save_device(data['name'], data['nodeid'], data['pluginid'], data['location']).addCallback(self.device_added)

        return NOT_DONE_YET