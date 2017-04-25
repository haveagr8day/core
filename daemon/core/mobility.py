"""
mobility.py: mobility helpers for moving nodes and calculating wireless range.
"""

import heapq
import math
import os
import subprocess
import threading
import time

from core.conf import Configurable
from core.conf import ConfigurableManager
from core.coreobj import PyCoreNode
from core.data import EventData, LinkData
from core.enumerations import ConfigDataTypes
from core.enumerations import EventTypes
from core.enumerations import LinkTypes
from core.enumerations import MessageFlags
from core.enumerations import MessageTypes
from core.enumerations import NodeTlvs
from core.enumerations import RegisterTlvs
from core.misc import log
from core.misc.ipaddress import IpAddress

logger = log.get_logger(__name__)


class MobilityManager(ConfigurableManager):
    """
    Member of session class for handling configuration data for mobility and
    range models.
    """
    name = "MobilityManager"
    config_type = RegisterTlvs.WIRELESS.value

    def __init__(self, session):
        """
        Creates a MobilityManager instance.

        :param core.session.Session session: session this manager is tied to
        :return: nothing
        """
        ConfigurableManager.__init__(self)
        self.session = session
        # configurations for basic range, indexed by WLAN node number, are
        # stored in self.configs
        # mapping from model names to their classes
        self._modelclsmap = {
            BasicRangeModel.name: BasicRangeModel,
            Ns2ScriptedMobility.name: Ns2ScriptedMobility
        }
        # dummy node objects for tracking position of nodes on other servers
        self.phys = {}
        self.physnets = {}
        self.session.broker.handlers += (self.physnodehandlelink,)

    def startup(self, node_ids=None):
        """
        Session is transitioning from instantiation to runtime state.
        Instantiate any mobility models that have been configured for a WLAN.
        """
        if node_ids is None:
            node_ids = self.configs.keys()

        for node_id in node_ids:
            logger.info("checking mobility startup for node: %s", node_id)

            try:
                node = self.session.get_object(node_id)
            except KeyError:
                logger.warn("skipping mobility configuration for unknown node %d." % node_id)
                continue

            if node_id not in self.configs:
                logger.warn("missing mobility configuration for node %d." % node_id)
                continue

            v = self.configs[node_id]

            for model in v:
                try:
                    logger.info("setting mobility model to node: %s", model)
                    cls = self._modelclsmap[model[0]]
                    node.setmodel(cls, model[1])
                except KeyError:
                    logger.warn("skipping mobility configuration for unknown model '%s'" % model[0])
                    continue

            if self.session.master:
                self.installphysnodes(node)

            if node.mobility:
                self.session.event_loop.add_event(0.0, node.mobility.startup)

    def reset(self):
        """
        Reset all configs.
        """
        self.clearconfig(nodenum=None)

    def setconfig(self, node_id, config_type, values):
        """
        Normal setconfig() with check for run-time updates for WLANs.
        """
        super(MobilityManager, self).setconfig(node_id, config_type, values)
        if self.session is None:
            return
        if self.session.state == EventTypes.RUNTIME_STATE.value:
            try:
                node = self.session.get_object(node_id)
                node.updatemodel(config_type, values)
            except KeyError:
                logger.exception("Skipping mobility configuration for unknown node %d.", node_id)

    def handleevent(self, event_data):
        """
        Handle an Event Message used to start, stop, or pause
        mobility scripts for a given WlanNode.
        """
        event_type = event_data.event_type
        node_id = event_data.node
        name = event_data.name

        try:
            node = self.session.get_object(node_id)
        except KeyError:
            logger.exception("Ignoring event for model '%s', unknown node '%s'", name, node_id)
            return

        # name is e.g. "mobility:ns2script"
        models = name[9:].split(',')
        for model in models:
            try:
                cls = self._modelclsmap[model]
            except KeyError:
                logger.warn("Ignoring event for unknown model '%s'", model)
                continue

            if cls.config_type in [RegisterTlvs.WIRELESS.value, RegisterTlvs.MOBILITY.value]:
                model = node.mobility
            else:
                continue

            if model is None:
                logger.warn("Ignoring event, %s has no model", node.name)
                continue

            if cls.name != model.name:
                logger.warn("Ignoring event for %s wrong model %s,%s", node.name, cls.name, model.name)
                continue

            if event_type == EventTypes.STOP.value or event_type == EventTypes.RESTART.value:
                model.stop(move_initial=True)
            if event_type == EventTypes.START.value or event_type == EventTypes.RESTART.value:
                model.start()
            if event_type == EventTypes.PAUSE.value:
                model.pause()

    def sendevent(self, model):
        """
        Send an event message on behalf of a mobility model.
        This communicates the current and end (max) times to the GUI.
        """
        event_type = EventTypes.NONE.value
        if model.state == model.STATE_STOPPED:
            event_type = EventTypes.STOP.value
        elif model.state == model.STATE_RUNNING:
            event_type = EventTypes.START.value
        elif model.state == model.STATE_PAUSED:
            event_type = EventTypes.PAUSE.value

        data = "start=%d" % int(model.lasttime - model.timezero)
        data += " end=%d" % int(model.endtime)

        event_data = EventData(
            node=model.objid,
            event_type=event_type,
            name="mobility:%s" % model.name,
            data=data,
            time="%s" % time.time()
        )

        self.session.broadcast_event(event_data)

    def updatewlans(self, moved, moved_netifs):
        """
        A mobility script has caused nodes in the 'moved' list to move.
        Update every WlanNode. This saves range calculations if the model
        were to recalculate for each individual node movement.
        """
        for nodenum in self.configs:
            try:
                n = self.session.get_object(nodenum)
            except KeyError:
                continue
            if n.model:
                n.model.update(moved, moved_netifs)

    def addphys(self, netnum, node):
        """
        Keep track of PhysicalNodes and which network they belong to.
        """
        nodenum = node.objid
        self.phys[nodenum] = node
        if netnum not in self.physnets:
            self.physnets[netnum] = [nodenum, ]
        else:
            self.physnets[netnum].append(nodenum)

    def physnodehandlelink(self, message):
        """
        Broker handler. Snoop Link add messages to get
        node numbers of PhyiscalNodes and their nets.
        Physical nodes exist only on other servers, but a shadow object is
        created here for tracking node position.
        """
        if message.message_type == MessageTypes.LINK.value and message.flags & MessageFlags.ADD.value:
            nn = message.node_numbers()
            # first node is always link layer node in Link add message
            if nn[0] not in self.session.broker.network_nodes:
                return
            if nn[1] in self.session.broker.physical_nodes:
                # record the fact that this PhysicalNode is linked to a net
                dummy = PyCoreNode(session=self.session, objid=nn[1],
                                   name="n%d" % nn[1], start=False)
                self.addphys(nn[0], dummy)

    def physnodeupdateposition(self, message):
        """
        Snoop node messages belonging to physical nodes. The dummy object
        in self.phys[] records the node position.
        """
        nodenum = message.node_numbers()[0]
        try:
            dummy = self.phys[nodenum]
            nodexpos = message.get_tlv(NodeTlvs.X_POSITION.value)
            nodeypos = message.get_tlv(NodeTlvs.Y_POSITION.value)
            dummy.setposition(nodexpos, nodeypos, None)
        except KeyError:
            logger.exception("error retrieving physical node: %s", nodenum)

    def installphysnodes(self, net):
        """
        After installing a mobility model on a net, include any physical
        nodes that we have recorded. Use the GreTap tunnel to the physical node
        as the node's interface.
        """
        try:
            nodenums = self.physnets[net.objid]
        except KeyError:
            logger.exception("error retriving physical net object")
            return

        for nodenum in nodenums:
            node = self.phys[nodenum]
            servers = self.session.broker.getserversbynode(nodenum)
            (host, port, sock) = self.session.broker.getserver(servers[0])
            netif = self.session.broker.gettunnel(net.objid, IpAddress.to_int(host))
            node.addnetif(netif, 0)
            netif.node = node
            (x, y, z) = netif.node.position.get()
            netif.poshook(netif, x, y, z)


class WirelessModel(Configurable):
    """
    Base class used by EMANE models and the basic range model.
    Used for managing arbitrary configuration parameters.
    """
    config_type = RegisterTlvs.WIRELESS.value
    bitmap = None
    position_callback = None

    def __init__(self, session, object_id, values=None):
        Configurable.__init__(self, session, object_id)
        # 'values' can be retrieved from a ConfigurableManager, or used here
        # during initialization, depending on the model.

    def all_link_data(self, flags):
        """
        May be used if the model can populate the GUI with wireless (green)
        link lines.
        """
        return []

    def update(self, moved, moved_netifs):
        raise NotImplementedError

    def updateconfig(self, values):
        """
        For run-time updates of model config.
        Returns True when self._positioncallback() and self.setlinkparams()
        should be invoked.
        """
        return False


class BasicRangeModel(WirelessModel):
    """
    Basic Range wireless model, calculates range between nodes and links
    and unlinks nodes based on this distance. This was formerly done from
    the GUI.
    """
    name = "basic_range"

    # configuration parameters are
    #  ( 'name', 'type', 'default', 'possible-value-list', 'caption')
    config_matrix = [
        ("range", ConfigDataTypes.UINT32.value, '275',
         '', 'wireless range (pixels)'),
        ("bandwidth", ConfigDataTypes.UINT32.value, '54000',
         '', 'bandwidth (bps)'),
        ("jitter", ConfigDataTypes.FLOAT.value, '0.0',
         '', 'transmission jitter (usec)'),
        ("delay", ConfigDataTypes.FLOAT.value, '5000.0',
         '', 'transmission delay (usec)'),
        ("error", ConfigDataTypes.FLOAT.value, '0.0',
         '', 'error rate (%)'),
    ]

    # value groupings
    config_groups = "Basic Range Parameters:1-%d" % len(config_matrix)

    def __init__(self, session, object_id, values=None):
        """
        Range model is only instantiated during runtime.
        """
        super(BasicRangeModel, self).__init__(session=session, object_id=object_id)
        self.wlan = session.get_object(object_id)
        self._netifs = {}
        self._netifslock = threading.Lock()
        if values is None:
            values = session.mobility.getconfig(object_id, self.name, self.getdefaultvalues())[1]
        self.range = float(self.valueof("range", values))
        logger.info("Basic range model configured for WLAN %d using range %d", object_id, self.range)
        self.valuestolinkparams(values)

    def valuestolinkparams(self, values):
        self.bw = int(self.valueof("bandwidth", values))
        if self.bw == 0.0:
            self.bw = None
        self.delay = float(self.valueof("delay", values))
        if self.delay == 0.0:
            self.delay = None
        self.loss = float(self.valueof("error", values))
        if self.loss == 0.0:
            self.loss = None
        self.jitter = float(self.valueof("jitter", values))
        if self.jitter == 0.0:
            self.jitter = None

    @classmethod
    def configure_mob(cls, session, config_data):
        """
        Handle configuration messages for setting up a model.
        Pass the MobilityManager object as the manager object.

        :param session: current session calling function
        :param core.conf.ConfigData config_data: configuration data for carrying out a configuration
        """
        return cls.configure(session.mobility, config_data)

    def setlinkparams(self):
        """
        Apply link parameters to all interfaces. This is invoked from
        WlanNode.setmodel() after the position callback has been set.
        """
        with self._netifslock:
            for netif in self._netifs:
                self.wlan.linkconfig(netif, bw=self.bw, delay=self.delay,
                                     loss=self.loss, duplicate=None,
                                     jitter=self.jitter)

    def get_position(self, netif):
        with self._netifslock:
            return self._netifs[netif]

    def set_position(self, netif, x=None, y=None, z=None):
        """
        A node has moved; given an interface, a new (x,y,z) position has
        been set; calculate the new distance between other nodes and link or
        unlink node pairs based on the configured range.
        """
        # print "set_position(%s, x=%s, y=%s, z=%s)" % (netif.localname, x, y, z)
        self._netifslock.acquire()
        self._netifs[netif] = (x, y, z)
        if x is None or y is None:
            self._netifslock.release()
            return
        for netif2 in self._netifs:
            self.calclink(netif, netif2)
        self._netifslock.release()

    position_callback = set_position

    def update(self, moved, moved_netifs):
        """
        Node positions have changed without recalc. Update positions from
        node.position, then re-calculate links for those that have moved.
        Assumes bidirectional links, with one calculation per node pair, where
        one of the nodes has moved.
        """
        with self._netifslock:
            while len(moved_netifs):
                netif = moved_netifs.pop()
                (nx, ny, nz) = netif.node.getposition()
                if netif in self._netifs:
                    self._netifs[netif] = (nx, ny, nz)
                for netif2 in self._netifs:
                    if netif2 in moved_netifs:
                        continue
                    self.calclink(netif, netif2)

    def calclink(self, netif, netif2):
        """
        Helper used by set_position() and update() to
        calculate distance between two interfaces and perform
        linking/unlinking. Sends link/unlink messages and updates the
        WlanNode's linked dict.
        """
        if netif == netif2:
            return
        try:
            (x, y, z) = self._netifs[netif]
            (x2, y2, z2) = self._netifs[netif2]
        except KeyError:
            return
        if x2 is None or y2 is None:
            return

        d = self.calcdistance((x, y, z), (x2, y2, z2))
        # ordering is important, to keep the wlan._linked dict organized
        a = min(netif, netif2)
        b = max(netif, netif2)
        try:
            self.wlan._linked_lock.acquire()
            linked = self.wlan.linked(a, b)
        except KeyError:
            return
        finally:
            self.wlan._linked_lock.release()
        if d > self.range:
            if linked:
                self.wlan.unlink(a, b)
                self.sendlinkmsg(a, b, unlink=True)
        else:
            if not linked:
                self.wlan.link(a, b)
                self.sendlinkmsg(a, b)

    @staticmethod
    def calcdistance(p1, p2):
        """
        Calculate the distance between two three-dimensional points.
        """
        a = p1[0] - p2[0]
        b = p1[1] - p2[1]
        c = 0
        if p1[2] is not None and p2[2] is not None:
            c = p1[2] - p2[2]
        return math.hypot(math.hypot(a, b), c)

    def updateconfig(self, values):
        """
        Configuration has changed during runtime.
        MobilityManager.setconfig() -> WlanNode.updatemodel() ->
        WirelessModel.updateconfig()
        """
        self.valuestolinkparams(values)
        self.range = float(self.valueof("range", values))
        return True

    def create_link_data(self, interface1, interface2, message_type):
        """
        Create a wireless link/unlink data message.
        """

        return LinkData(
            message_type=message_type,
            node1_id=interface1.node.objid,
            node2_id=interface2.node.objid,
            network_id=self.wlan.objid,
            link_type=LinkTypes.WIRELESS.value
        )

    def sendlinkmsg(self, netif, netif2, unlink=False):
        """
        Send a wireless link/unlink API message to the GUI.
        """
        if unlink:
            message_type = MessageFlags.DELETE.value
        else:
            message_type = MessageFlags.ADD.value

        link_data = self.create_link_data(netif, netif2, message_type)
        self.session.broadcast_link(link_data)

        # TODO: account for SDT wanting to listen as well
        # self.session.sdt.updatelink(netif.node.objid, netif2.node.objid, flags, wireless=True)

    def all_link_data(self, flags):
        """
        Return a list of wireless link messages for when the GUI reconnects.
        """
        all_links = []
        with self.wlan._linked_lock:
            for a in self.wlan._linked:
                for b in self.wlan._linked[a]:
                    if self.wlan._linked[a][b]:
                        all_links.append(self.create_link_data(a, b, flags))
        return all_links


class WayPointMobility(WirelessModel):
    """
    Abstract class for mobility models that set node waypoints.
    """
    name = "waypoint"
    config_type = RegisterTlvs.MOBILITY.value

    STATE_STOPPED = 0
    STATE_RUNNING = 1
    STATE_PAUSED = 2

    class WayPoint(object):
        def __init__(self, time, nodenum, coords, speed):
            self.time = time
            self.nodenum = nodenum
            self.coords = coords
            self.speed = speed

        def __cmp__(self, other):
            tmp = cmp(self.time, other.time)
            if tmp == 0:
                tmp = cmp(self.nodenum, other.nodenum)
            return tmp

    def __init__(self, session, object_id, values=None):
        super(WayPointMobility, self).__init__(session=session, object_id=object_id, values=values)
        self.state = self.STATE_STOPPED
        self.queue = []
        self.queue_copy = []
        self.points = {}
        self.initial = {}
        self.lasttime = None
        self.endtime = None
        self.wlan = session.get_object(object_id)
        # these are really set in child class via confmatrix
        self.loop = False
        self.refresh_ms = 50
        # flag whether to stop scheduling when queue is empty
        #  (ns-3 sets this to False as new waypoints may be added from trace)
        self.empty_queue_stop = True

    def runround(self):
        """
        Advance script time and move nodes.
        """
        if self.state != self.STATE_RUNNING:
            return
        t = self.lasttime
        self.lasttime = time.time()
        now = self.lasttime - self.timezero
        dt = self.lasttime - t
        # print "runround(now=%.2f, dt=%.2f)" % (now, dt)

        # keep current waypoints up-to-date
        self.updatepoints(now)

        if not len(self.points):
            if len(self.queue):
                # more future waypoints, allow time for self.lasttime update
                nexttime = self.queue[0].time - now
                if nexttime > (0.001 * self.refresh_ms):
                    nexttime -= 0.001 * self.refresh_ms
                self.session.evq.add_event(nexttime, self.runround)
                return
            else:
                # no more waypoints or queued items, loop?
                if not self.empty_queue_stop:
                    # keep running every refresh_ms, even with empty queue
                    self.session.evq.add_event(0.001 * self.refresh_ms, self.runround)
                    return
                if not self.loopwaypoints():
                    return self.stop(move_initial=False)
                if not len(self.queue):
                    # prevent busy loop
                    return
                return self.run()

        # only move netifs attached to self.wlan, or all nodenum in script?
        moved = []
        moved_netifs = []
        for netif in self.wlan.netifs():
            node = netif.node
            if self.movenode(node, dt):
                moved.append(node)
                moved_netifs.append(netif)

        # calculate all ranges after moving nodes; this saves calculations
        # self.wlan.model.update(moved)
        self.session.mobility.updatewlans(moved, moved_netifs)

        # TODO: check session state
        self.session.evq.add_event(0.001 * self.refresh_ms, self.runround)

    def run(self):
        self.timezero = time.time()
        self.lasttime = self.timezero - (0.001 * self.refresh_ms)
        self.movenodesinitial()
        self.runround()
        self.session.mobility.sendevent(self)

    def movenode(self, node, dt):
        """
        Calculate next node location and update its coordinates.
        Returns True if the node's position has changed.
        """
        if node.objid not in self.points:
            return False
        x1, y1, z1 = node.getposition()
        x2, y2, z2 = self.points[node.objid].coords
        speed = self.points[node.objid].speed
        # instantaneous move (prevents dx/dy == 0.0 below)
        if speed == 0:
            self.setnodeposition(node, x2, y2, z2)
            del self.points[node.objid]
            return True
        # speed can be a velocity vector (ns3 mobility) or speed value
        if isinstance(speed, (float, int)):
            # linear speed value
            alpha = math.atan2(y2 - y1, x2 - x1)
            sx = speed * math.cos(alpha)
            sy = speed * math.sin(alpha)
        else:
            # velocity vector
            sx = speed[0]
            sy = speed[1]

        # calculate dt * speed = distance moved
        dx = sx * dt
        dy = sy * dt
        # prevent overshoot
        if abs(dx) > abs(x2 - x1):
            dx = x2 - x1
        if abs(dy) > abs(y2 - y1):
            dy = y2 - y1
        if dx == 0.0 and dy == 0.0:
            if self.endtime < (self.lasttime - self.timezero):
                # the last node to reach the last waypoint determines this
                # script's endtime
                self.endtime = self.lasttime - self.timezero
            del self.points[node.objid]
            return False
        # print "node %s dx,dy= <%s, %d>" % (node.name, dx, dy)
        if (x1 + dx) < 0.0:
            dx = 0.0 - x1
        if (y1 + dy) < 0.0:
            dy = 0.0 - y1
        self.setnodeposition(node, x1 + dx, y1 + dy, z1)
        return True

    def movenodesinitial(self):
        """
        Move nodes to their initial positions. Then calculate the ranges.
        """
        moved = []
        moved_netifs = []
        for netif in self.wlan.netifs():
            node = netif.node
            if node.objid not in self.initial:
                continue
            (x, y, z) = self.initial[node.objid].coords
            self.setnodeposition(node, x, y, z)
            moved.append(node)
            moved_netifs.append(netif)
        # self.wlan.model.update(moved)
        self.session.mobility.updatewlans(moved, moved_netifs)

    def addwaypoint(self, time, nodenum, x, y, z, speed):
        """
        Waypoints are pushed to a heapq, sorted by time.
        """
        # print "addwaypoint: %s %s %s,%s,%s %s" % (time, nodenum, x, y, z, speed)
        wp = self.WayPoint(time, nodenum, coords=(x, y, z), speed=speed)
        heapq.heappush(self.queue, wp)

    def addinitial(self, nodenum, x, y, z):
        """
        Record initial position in a dict.
        """
        wp = self.WayPoint(0, nodenum, coords=(x, y, z), speed=0)
        self.initial[nodenum] = wp

    def updatepoints(self, now):
        """
        Move items from self.queue to self.points when their time has come.
        """
        while len(self.queue):
            if self.queue[0].time > now:
                break
            wp = heapq.heappop(self.queue)
            self.points[wp.nodenum] = wp

    def copywaypoints(self):
        """
        Store backup copy of waypoints for looping and stopping.
        """
        self.queue_copy = list(self.queue)

    def loopwaypoints(self):
        """
        Restore backup copy of waypoints when looping.
        """
        self.queue = list(self.queue_copy)
        return self.loop

    def setnodeposition(self, node, x, y, z):
        """
        Helper to move a node, notify any GUI (connected session handlers),
        without invoking the interface poshook callback that may perform
        range calculation.
        """
        # this would cause PyCoreNetIf.poshook() callback (range calculation)
        # node.setposition(x, y, z)
        node.position.set(x, y, z)
        node_data = node.data(message_type=0)
        self.session.broadcast_node(node_data)

        # TODO: determine how to add handler for SDT
        # self.session.sdt.updatenode(node.objid, flags=0, x=x, y=y, z=z)

    def setendtime(self):
        """
        Set self.endtime to the time of the last waypoint in the queue of
        waypoints. This is just an estimate. The endtime will later be
        adjusted, after one round of the script has run, to be the time
        that the last moving node has reached its final waypoint.
        """
        try:
            self.endtime = self.queue[-1].time
        except IndexError:
            self.endtime = 0

    def start(self):
        """
        Run the script from the beginning or unpause from where it
        was before.
        """
        laststate = self.state
        self.state = self.STATE_RUNNING
        if laststate == self.STATE_STOPPED or laststate == self.STATE_RUNNING:
            self.loopwaypoints()
            self.timezero = 0
            self.lasttime = 0
            self.run()
        elif laststate == self.STATE_PAUSED:
            now = time.time()
            self.timezero += now - self.lasttime
            self.lasttime = now - (0.001 * self.refresh_ms)
            self.runround()

    def stop(self, move_initial=True):
        """
        Stop the script and move nodes to initial positions.
        """
        self.state = self.STATE_STOPPED
        self.loopwaypoints()
        self.timezero = 0
        self.lasttime = 0
        if move_initial:
            self.movenodesinitial()
        self.session.mobility.sendevent(self)

    def pause(self):
        """
        Pause the script; pause time is stored to self.lasttime.
        """
        self.state = self.STATE_PAUSED
        self.lasttime = time.time()


class Ns2ScriptedMobility(WayPointMobility):
    """
    Handles the ns-2 script format, generated by scengen/setdest or
    BonnMotion.
    """
    name = "ns2script"

    config_matrix = [
        ("file", ConfigDataTypes.STRING.value, '',
         '', 'mobility script file'),
        ("refresh_ms", ConfigDataTypes.UINT32.value, '50',
         '', 'refresh time (ms)'),
        ("loop", ConfigDataTypes.BOOL.value, '1',
         'On,Off', 'loop'),
        ("autostart", ConfigDataTypes.STRING.value, '',
         '', 'auto-start seconds (0.0 for runtime)'),
        ("map", ConfigDataTypes.STRING.value, '',
         '', 'node mapping (optional, e.g. 0:1,1:2,2:3)'),
        ("script_start", ConfigDataTypes.STRING.value, '',
         '', 'script file to run upon start'),
        ("script_pause", ConfigDataTypes.STRING.value, '',
         '', 'script file to run upon pause'),
        ("script_stop", ConfigDataTypes.STRING.value, '',
         '', 'script file to run upon stop'),
    ]
    config_groups = "ns-2 Mobility Script Parameters:1-%d" % len(config_matrix)

    def __init__(self, session, object_id, values=None):
        super(Ns2ScriptedMobility, self).__init__(session=session, object_id=object_id, values=values)
        self._netifs = {}
        self._netifslock = threading.Lock()
        if values is None:
            values = session.mobility.getconfig(object_id, self.name, self.getdefaultvalues())[1]
        self.file = self.valueof("file", values)
        self.refresh_ms = int(self.valueof("refresh_ms", values))
        self.loop = self.valueof("loop", values).lower() == "on"
        self.autostart = self.valueof("autostart", values)
        self.parsemap(self.valueof("map", values))
        self.script_start = self.valueof("script_start", values)
        self.script_pause = self.valueof("script_pause", values)
        self.script_stop = self.valueof("script_stop", values)
        logger.info("ns-2 scripted mobility configured for WLAN %d using file: %s", object_id, self.file)
        self.readscriptfile()
        self.copywaypoints()
        self.setendtime()

    @classmethod
    def configure_mob(cls, session, config_data):
        """
        Handle configuration messages for setting up a model.
        Pass the MobilityManager object as the manager object.

        :param session: current session calling function
        :param core.conf.ConfigData config_data: configuration data for carrying out a configuration
        """
        return cls.configure(session.mobility, config_data)

    def readscriptfile(self):
        """
        Read in mobility script from a file. This adds waypoints to a
        priority queue, sorted by waypoint time. Initial waypoints are
        stored in a separate dict.
        """
        filename = self.findfile(self.file)
        try:
            f = open(filename, 'r')
        except IOError:
            logger.exception("ns-2 scripted mobility failed to load file  '%s'", self.file)
            return
        logger.info("reading ns-2 script file: %s" % filename)
        ln = 0
        ix = iy = iz = None
        inodenum = None
        for line in f:
            ln += 1
            if line[:2] != '$n':
                continue
            try:
                if line[:8] == "$ns_ at ":
                    if ix is not None and iy is not None:
                        self.addinitial(self.map(inodenum), ix, iy, iz)
                        ix = iy = iz = None
                    # waypoints:
                    #    $ns_ at 1.00 "$node_(6) setdest 500.0 178.0 25.0"
                    parts = line.split()
                    time = float(parts[2])
                    nodenum = parts[3][1 + parts[3].index('('):parts[3].index(')')]
                    x = float(parts[5])
                    y = float(parts[6])
                    z = None
                    speed = float(parts[7].strip('"'))
                    self.addwaypoint(time, self.map(nodenum), x, y, z, speed)
                elif line[:7] == "$node_(":
                    # initial position (time=0, speed=0):
                    #    $node_(6) set X_ 780.0
                    parts = line.split()
                    time = 0.0
                    nodenum = parts[0][1 + parts[0].index('('):parts[0].index(')')]
                    if parts[2] == 'X_':
                        if ix is not None and iy is not None:
                            self.addinitial(self.map(inodenum), ix, iy, iz)
                            ix = iy = iz = None
                        ix = float(parts[3])
                    elif parts[2] == 'Y_':
                        iy = float(parts[3])
                    elif parts[2] == 'Z_':
                        iz = float(parts[3])
                        self.addinitial(self.map(nodenum), ix, iy, iz)
                        ix = iy = iz = None
                    inodenum = nodenum
                else:
                    raise ValueError
            except ValueError:
                logger.exception("skipping line %d of file %s '%s'", ln, self.file, line)
                continue
        if ix is not None and iy is not None:
            self.addinitial(self.map(inodenum), ix, iy, iz)

    def findfile(self, fn):
        """
        Locate a script file. If the specified file doesn't exist, look in the
        same directory as the scenario file (session.filename), or in the default
        configs directory (~/.core/configs). This allows for sample files without
        absolute pathnames.
        """
        if os.path.exists(fn):
            return fn
        if self.session.filename is not None:
            d = os.path.dirname(self.session.filename)
            sessfn = os.path.join(d, fn)
            if os.path.exists(sessfn):
                return sessfn
        if self.session.user is not None:
            userfn = os.path.join('/home', self.session.user, '.core', 'configs', fn)
            if os.path.exists(userfn):
                return userfn
        return fn

    def parsemap(self, mapstr):
        """
        Parse a node mapping string, given as a configuration parameter.
        """
        self.nodemap = {}
        if mapstr.strip() == '':
            return
        for pair in mapstr.split(','):
            parts = pair.split(':')
            try:
                if len(parts) != 2:
                    raise ValueError
                self.nodemap[int(parts[0])] = int(parts[1])
            except ValueError:
                logger.warn("ns-2 mobility node map error")
                return

    def map(self, nodenum):
        """
        Map one node number (from a script file) to another.
        """
        nodenum = int(nodenum)
        try:
            return self.nodemap[nodenum]
        except KeyError:
            return nodenum

    def startup(self):
        """
        Start running the script if autostart is enabled.
        Move node to initial positions when any autostart time is specified.
        Ignore the script if autostart is an empty string (can still be
        started via GUI controls).
        """
        if self.autostart == '':
            logger.info("not auto-starting ns-2 script for %s" % self.wlan.name)
            return
        try:
            t = float(self.autostart)
        except ValueError:
            logger.exception("Invalid auto-start seconds specified '%s' for %s", self.autostart, self.wlan.name)
            return
        self.movenodesinitial()
        logger.info("scheduling ns-2 script for %s autostart at %s" % (self.wlan.name, t))
        self.state = self.STATE_RUNNING
        self.session.evq.add_event(t, self.run)

    def start(self):
        """
        Handle the case when un-paused.
        """
        laststate = self.state
        super(Ns2ScriptedMobility, self).start()
        if laststate == self.STATE_PAUSED:
            self.statescript("unpause")

    def run(self):
        """
        Start is pressed or autostart is triggered.
        """
        super(Ns2ScriptedMobility, self).run()
        self.statescript("run")

    def pause(self):
        super(Ns2ScriptedMobility, self).pause()
        self.statescript("pause")

    def stop(self, move_initial=True):
        super(Ns2ScriptedMobility, self).stop(move_initial=move_initial)
        self.statescript("stop")

    def statescript(self, typestr):
        filename = None
        if typestr == "run" or typestr == "unpause":
            filename = self.script_start
        elif typestr == "pause":
            filename = self.script_pause
        elif typestr == "stop":
            filename = self.script_stop
        if filename is None or filename == '':
            return
        filename = self.findfile(filename)
        try:
            subprocess.check_call(
                ["/bin/sh", filename, typestr],
                cwd=self.session.sessiondir,
                env=self.session.get_environment()
            )
        except subprocess.CalledProcessError:
            logger.exception("Error running script '%s' for WLAN state %s", filename, typestr)
