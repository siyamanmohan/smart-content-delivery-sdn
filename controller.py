"""
Smart Content Delivery System - Ryu SDN controller (OpenFlow 1.3)

What it does
------------
* Clients connect to ONE virtual IP (VIP 10.0.0.100:8080). They never know the
  real server addresses.
* On the first packet of every TCP connection the controller picks a content
  server, then installs two flows in the switch:
      client -> VIP      : rewrite dst MAC/IP to the chosen server
      server -> client   : rewrite src MAC/IP back to the VIP
  (transparent NAT, so the sockets code stays plain TCP).
* Servers send a UDP heartbeat (broadcast, port 9999) with their name and
  number of active connections. The heartbeat reaches the controller as a
  packet-in -> this is the socket <-> SDN integration. It gives:
      - automatic server discovery (MAC / IP / switch port are learned)
      - server availability (no heartbeat for HB_TIMEOUT s => server is DOWN)
* Port-status events (link down) mark a server DOWN instantly.
* Port statistics are polled every second -> per-server utilisation.
* When a server goes DOWN its flows are deleted, so the next packet of each
  affected connection may fail. Client retries open new TCP connections
  which can be assigned to a healthy server.

Modes (environment variable LB_MODE)
------------------------------------
  dynamic (default) : picks the lowest-cost alive server (load + conns + delay)
  static            : BASELINE - always server1, ignores health and load

Run:   ryu-manager controller.py
       LB_MODE=static ryu-manager controller.py
"""
import json
import os
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import arp, ethernet, ether_types, ipv4, packet, tcp, udp
from ryu.ofproto import ofproto_v1_3

# ---------------------------------------------------------------- config ----
VIP_IP = '10.0.0.100'
VIP_MAC = '00:00:00:00:00:64'
SERVICE_PORT = 8080
HB_PORT = 9999
HB_TIMEOUT = 3.0          # seconds without heartbeat => server DOWN
STATS_INTERVAL = 1.0      # seconds between port-stat polls
FLOW_IDLE = 10            # idle timeout (s) of per-connection flows
COOKIE_FWD = 0xF0         # tags client->server flows so we get FlowRemoved

MODE = os.environ.get('LB_MODE', 'dynamic').lower()

# Link capacity (Mbps) and one-way delay (ms) of each server link.
# MUST match topo.py.
SERVER_META = {
    'server1': {'capacity_mbps': 30, 'delay_ms': 2},
    'server2': {'capacity_mbps': 20, 'delay_ms': 10},
}

# cost = W_UTIL*utilisation + W_CONN*(conns / weight) + W_DELAY*delay_ms
W_UTIL, W_CONN, W_DELAY = 1.0, 0.1, 0.02


class SmartContentController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SmartContentController, self).__init__(*args, **kwargs)
        self.datapaths = {}      # dpid -> datapath
        self.mac_to_port = {}    # dpid -> {mac: port}
        self.servers = {}        # name -> state dict
        self.sessions = {}       # (client_ip, client_tcp_port) -> server name
        self.monitor_thread = hub.spawn(self._monitor)
        self.logger.info('=== Smart Content controller started, MODE=%s ===',
                         MODE)

    # ------------------------------------------------------ switch setup ----
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        ofp, p = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        # table-miss: send whole packet to the controller
        actions = [p.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                     ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, p.OFPMatch(), actions)
        self.logger.info('Switch %s connected', dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    # ----------------------------------------------------------- helpers ----
    def _add_flow(self, dp, priority, match, actions, idle=0, flags=0,
                  cookie=0):
        ofp, p = dp.ofproto, dp.ofproto_parser
        inst = [p.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(p.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                 instructions=inst, idle_timeout=idle,
                                 flags=flags, cookie=cookie))

    def _delete_flows(self, dp, match=None, out_port=None):
        ofp, p = dp.ofproto, dp.ofproto_parser
        dp.send_msg(p.OFPFlowMod(
            datapath=dp, command=ofp.OFPFC_DELETE,
            out_port=ofp.OFPP_ANY if out_port is None else out_port,
            out_group=ofp.OFPG_ANY,
            match=match if match is not None else p.OFPMatch()))

    def _packet_out(self, dp, in_port, actions, data):
        ofp, p = dp.ofproto, dp.ofproto_parser
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                   in_port=in_port, actions=actions,
                                   data=data))

    # ------------------------------------------------------- packet-in ------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        self.mac_to_port.setdefault(dp.id, {})[eth.src] = in_port

        a = pkt.get_protocol(arp.arp)
        if a:
            if a.opcode == arp.ARP_REQUEST and a.dst_ip == VIP_IP:
                self._reply_vip_arp(dp, in_port, eth, a)
            else:
                self._l2_forward(dp, in_port, eth, msg)
            return

        ip = pkt.get_protocol(ipv4.ipv4)
        if ip:
            u = pkt.get_protocol(udp.udp)
            if u and u.dst_port == HB_PORT:
                self._handle_heartbeat(dp, in_port, eth, ip, pkt)
                return
            t = pkt.get_protocol(tcp.tcp)
            if t and ip.dst == VIP_IP and t.dst_port == SERVICE_PORT:
                self._handle_vip_tcp(dp, in_port, eth, ip, t, msg)
                return

        self._l2_forward(dp, in_port, eth, msg)

    # ------------------------------------------------- plain L2 switching ---
    def _l2_forward(self, dp, in_port, eth, msg):
        ofp, p = dp.ofproto, dp.ofproto_parser
        is_multicast = int(eth.dst.split(':')[0], 16) & 1
        out = self.mac_to_port.get(dp.id, {}).get(eth.dst)
        if is_multicast or out is None:
            out_port = ofp.OFPP_FLOOD
        else:
            out_port = out
            self._add_flow(dp, 10,
                           p.OFPMatch(in_port=in_port, eth_dst=eth.dst),
                           [p.OFPActionOutput(out_port)], idle=30)
        self._packet_out(dp, in_port, [p.OFPActionOutput(out_port)], msg.data)

    # ---------------------------------------------------- ARP for the VIP ---
    def _reply_vip_arp(self, dp, in_port, eth, a):
        ofp, p = dp.ofproto, dp.ofproto_parser
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=eth.ethertype,
                                           dst=eth.src, src=VIP_MAC))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=VIP_MAC,
                                 src_ip=VIP_IP, dst_mac=a.src_mac,
                                 dst_ip=a.src_ip))
        pkt.serialize()
        self._packet_out(dp, ofp.OFPP_CONTROLLER,
                         [p.OFPActionOutput(in_port)], pkt.data)

    # ---------------------------------------- heartbeat = socket -> SDN ------
    def _handle_heartbeat(self, dp, in_port, eth, ip, pkt):
        payload = pkt.protocols[-1]
        if not isinstance(payload, (bytes, bytearray)):
            return
        try:
            info = json.loads(bytes(payload).decode())
        except ValueError:
            return
        if not isinstance(info, dict):
            return
        name = info.get('name')
        active = info.get('active')
        if not isinstance(name, str) or name not in SERVER_META:
            return
        if type(active) is not int or active < 0:
            return
        meta = SERVER_META[name]
        s = self.servers.setdefault(name, {
            'name': name, 'alive': False, 'rx_rate': 0.0,
            'prev_rx': None, 'prev_t': None, 'reported': 0,
            'capacity_mbps': meta['capacity_mbps'],
            'delay_ms': meta['delay_ms']})
        s.update(ip=ip.src, mac=eth.src, port=in_port, dpid=dp.id,
                 last_hb=time.time(), reported=active)
        if not s['alive']:
            s['alive'] = True
            self.logger.warning('SERVER UP    %s ip=%s port=%s', name, ip.src,
                                in_port)

    # --------------------------------------------------- server selection ---
    def _conns(self, s):
        tracked = sum(1 for v in self.sessions.values() if v == s['name'])
        return max(tracked, s.get('reported', 0))

    def _cost(self, s):
        util = s['rx_rate'] / (s['capacity_mbps'] * 1e6)
        weight = s['capacity_mbps'] / 20.0
        return (W_UTIL * util + W_CONN * self._conns(s) / weight +
                W_DELAY * s['delay_ms'])

    def _select_server(self):
        if MODE == 'static':
            return self.servers.get('server1')      # baseline
        alive = [s for s in self.servers.values() if s['alive']]
        if not alive:
            return None
        return min(alive, key=self._cost)

    # ----------------------------------------- the dynamic load balancer ----
    def _handle_vip_tcp(self, dp, in_port, eth, ip, t, msg):
        p = dp.ofproto_parser
        key = (ip.src, t.src_port)
        name = self.sessions.get(key)
        srv = self.servers.get(name) if name else None
        if srv is None or (MODE != 'static' and not srv['alive']):
            # A replica cannot take over an established TCP byte stream.
            # Assign a backend only for an opening SYN, not a stale ACK.
            syn, ack = 0x02, 0x10
            if (t.bits & (syn | ack)) != syn:
                return
            costs = ' '.join('%s=%.3f' % (n, self._cost(s))
                             for n, s in sorted(self.servers.items())
                             if s['alive'])
            srv = self._select_server()
            if srv is None:
                self.logger.warning('NO SERVER AVAILABLE for %s:%s - dropping',
                                    ip.src, t.src_port)
                return
            self.sessions[key] = srv['name']
            self.logger.info('SELECT %s:%s -> %s   [cost %s]  mode=%s',
                             ip.src, t.src_port, srv['name'], costs, MODE)

        # client -> VIP  becomes  client -> server
        fwd_match = p.OFPMatch(in_port=in_port, eth_type=0x0800, ip_proto=6,
                               ipv4_src=ip.src, ipv4_dst=VIP_IP,
                               tcp_src=t.src_port, tcp_dst=t.dst_port)
        fwd_actions = [p.OFPActionSetField(eth_dst=srv['mac']),
                       p.OFPActionSetField(ipv4_dst=srv['ip']),
                       p.OFPActionOutput(srv['port'])]
        self._add_flow(dp, 100, fwd_match, fwd_actions, idle=FLOW_IDLE,
                       flags=dp.ofproto.OFPFF_SEND_FLOW_REM,
                       cookie=COOKIE_FWD)

        # server -> client  gets its source rewritten back to the VIP
        rev_match = p.OFPMatch(in_port=srv['port'], eth_type=0x0800,
                               ip_proto=6, ipv4_src=srv['ip'],
                               ipv4_dst=ip.src, tcp_src=t.dst_port,
                               tcp_dst=t.src_port)
        rev_actions = [p.OFPActionSetField(eth_src=VIP_MAC),
                       p.OFPActionSetField(ipv4_src=VIP_IP),
                       p.OFPActionOutput(in_port)]
        self._add_flow(dp, 100, rev_match, rev_actions, idle=FLOW_IDLE)

        # forward the packet that triggered all this
        self._packet_out(dp, in_port, fwd_actions, msg.data)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def _flow_removed(self, ev):
        msg = ev.msg
        if msg.cookie != COOKIE_FWD:
            return
        m = msg.match
        try:
            self.sessions.pop((m['ipv4_src'], m['tcp_src']), None)
        except KeyError:
            pass

    # ------------------------------------------------ failure handling ------
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        down = bool(msg.desc.state & ofp.OFPPS_LINK_DOWN) or \
            bool(msg.desc.config & ofp.OFPPC_PORT_DOWN)
        for s in self.servers.values():
            if (s.get('dpid') == dp.id and s.get('port') == msg.desc.port_no
                    and down and s['alive']):
                self._mark_down(s, 'link down on port %d' % msg.desc.port_no)

    def _mark_down(self, srv, reason):
        srv['alive'] = False
        srv['rx_rate'] = 0.0
        srv['prev_rx'] = None
        self.logger.warning('SERVER DOWN  %s (%s) -> flushing its flows',
                            srv['name'], reason)
        dp = self.datapaths.get(srv['dpid'])
        if dp:
            p = dp.ofproto_parser
            self._delete_flows(dp, out_port=srv['port'])           # to server
            self._delete_flows(dp, match=p.OFPMatch(in_port=srv['port']))
        for k in [k for k, v in self.sessions.items() if v == srv['name']]:
            del self.sessions[k]

    # ---------------------------------------------------- monitoring --------
    def _monitor(self):
        tick = 0
        while True:
            for dp in list(self.datapaths.values()):
                ofp, p = dp.ofproto, dp.ofproto_parser
                dp.send_msg(p.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
            now = time.time()
            for s in list(self.servers.values()):
                if s['alive'] and now - s['last_hb'] > HB_TIMEOUT:
                    self._mark_down(s, 'heartbeat timeout')
            tick += 1
            if tick % 5 == 0:
                for n, s in sorted(self.servers.items()):
                    self.logger.info(
                        'STATUS %s alive=%s util=%.1f%% sessions=%d reported=%d',
                        n, s['alive'],
                        100.0 * s['rx_rate'] / (s['capacity_mbps'] * 1e6),
                        sum(1 for v in self.sessions.values() if v == n),
                        s['reported'])
            hub.sleep(STATS_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):
        now = time.time()
        dpid = ev.msg.datapath.id
        for st in ev.msg.body:
            for s in self.servers.values():
                if s.get('dpid') == dpid and s.get('port') == st.port_no:
                    # rx on the server's switch port = content the server sends
                    if s['prev_rx'] is not None and now > s['prev_t']:
                        s['rx_rate'] = ((st.rx_bytes - s['prev_rx']) * 8.0 /
                                        (now - s['prev_t']))
                    s['prev_rx'] = st.rx_bytes
                    s['prev_t'] = now
