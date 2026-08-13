"""Mode-aware multicast DNS and DNS Service Discovery for BenchForge."""

import ipaddress
import socket
import struct
import threading
from typing import List, Optional, Sequence, Tuple

from .netutil import create_multicast_listener


class GatewayDiscoveryResponder:
    """RFC 6762/6763 DNS-SD responder for the active gateway persona.

    BenchForge is an emulator, not a certified LXI device. The E5810 persona
    publishes standard LXI, raw-SCPI, and VXI-11 service records. Prologix and
    AR488 publish distinct gateway types so browsers cannot mistake them for an
    LXI instrument.
    """

    MDNS_PORT = 5353
    MDNS_GROUP = "224.0.0.251"
    MDNS_TTL = 120

    TYPE_A = 1
    TYPE_PTR = 12
    TYPE_TXT = 16
    TYPE_SRV = 33
    TYPE_ANY = 255
    CLASS_IN = 1
    CACHE_FLUSH = 0x8000
    SERVICE_ENUMERATION = "_services._dns-sd._udp.local."

    def __init__(self, host_name="benchforge", model_name=None):
        self.host_name = self._normalise_host_name(host_name)
        self.model_name = model_name or "Keysight E5810A Gateway"
        self.bind_host = "127.0.0.1"
        self.address = "127.0.0.1"
        self.persona = "lxi"
        self.services: List[Tuple[str, int, Tuple[str, ...]]] = []
        self._is_running = False
        self._udp_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self.configure_lxi(self.bind_host, raw_port=5025, vxi11_port=1024)

    @staticmethod
    def _normalise_host_name(value):
        label = value.strip().rstrip(".")
        if label.lower().endswith(".local"):
            label = label[:-6]
        label = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in label)
        label = label.strip("-") or "benchforge"
        return label.encode("utf-8")[:63].decode("utf-8", errors="ignore")

    @property
    def fqdn(self):
        return "%s.local." % self.host_name

    @staticmethod
    def _txt_values(model, protocol, extra: Sequence[str] = ()):
        # LXI Device Specification 10.4.3.6 requires txtvers to be first.
        values = ("txtvers=1", "Manufacturer=BenchForge", "Model=%s" % model,
                  "SerialNumber=EMULATOR", "FirmwareVersion=1.0",
                  "protocol=%s" % protocol)
        return tuple(values + tuple(extra))

    def _set_configuration(self, persona, bind_host, model_name, services):
        if self._is_running:
            raise RuntimeError("stop mDNS before changing its advertised persona")
        self.persona = persona
        self.bind_host = bind_host.strip() or "0.0.0.0"
        self.address = self._advertised_address(self.bind_host)
        self.model_name = model_name
        self.services = list(services)

    def configure_prologix(self, bind_host, port):
        model = "Prologix GPIB-ETHERNET Controller"
        self._set_configuration(
            "prologix", bind_host, model,
            [("_prologix-gpib._tcp.local.", port,
              self._txt_values(model, "prologix",
                               ("GatewayFirmware=01.06.06.00",)))])

    def configure_ar488(self, bind_host, port):
        model = "AR488 / AR488Lan GPIB Gateway"
        self._set_configuration(
            "ar488", bind_host, model,
            [("_ar488-gpib._tcp.local.", port,
              self._txt_values(model, "ar488"))])

    def configure_lxi(self, bind_host, raw_port, vxi11_port):
        model = "Keysight E5810A LAN/GPIB Gateway"
        services = [
            ("_lxi._tcp.local.", raw_port, self._txt_values(model, None)),
            ("_scpi-raw._tcp.local.", raw_port, self._txt_values(model, None)),
        ]
        if vxi11_port is not None:
            services.append(("_vxi-11._tcp.local.", vxi11_port,
                             self._txt_values(model, None)))
        self._set_configuration("lxi", bind_host, model, services)

    @staticmethod
    def _advertised_address(bind_host):
        try:
            address = str(ipaddress.ip_address(bind_host))
            if address != "0.0.0.0" and ":" not in address:
                return address
        except ValueError:
            try:
                return socket.gethostbyname(bind_host)
            except OSError:
                pass
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((GatewayDiscoveryResponder.MDNS_GROUP,
                           GatewayDiscoveryResponder.MDNS_PORT))
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            probe.close()

    @staticmethod
    def _encode_name(name):
        encoded = bytearray()
        for label in name.rstrip(".").split("."):
            raw = label.encode("utf-8")
            if not raw or len(raw) > 63:
                raise ValueError("invalid DNS label in %r" % name)
            encoded.append(len(raw))
            encoded.extend(raw)
        encoded.append(0)
        return bytes(encoded)

    @classmethod
    def _read_name(cls, packet, offset):
        labels = []
        next_offset = None
        visited = set()
        while True:
            if offset >= len(packet) or offset in visited:
                raise ValueError("invalid compressed DNS name")
            visited.add(offset)
            length = packet[offset]
            if length == 0:
                offset += 1
                break
            if length & 0xC0 == 0xC0:
                if offset + 1 >= len(packet):
                    raise ValueError("truncated DNS compression pointer")
                if next_offset is None:
                    next_offset = offset + 2
                offset = ((length & 0x3F) << 8) | packet[offset + 1]
                continue
            if length & 0xC0 or offset + 1 + length > len(packet):
                raise ValueError("invalid DNS label")
            offset += 1
            labels.append(packet[offset:offset + length].decode("utf-8"))
            offset += length
        return ".".join(labels).lower() + ".", next_offset or offset

    @classmethod
    def _question_names(cls, packet):
        if len(packet) < 12:
            raise ValueError("truncated DNS header")
        transaction_id, flags, questions = struct.unpack(">HHH", packet[:6])
        if flags & 0x8000 or questions > 64:
            return transaction_id, False, []
        offset = 12
        result = []
        wants_unicast = False
        for _ in range(questions):
            name, offset = cls._read_name(packet, offset)
            if offset + 4 > len(packet):
                raise ValueError("truncated DNS question")
            qtype, qclass = struct.unpack(">HH", packet[offset:offset + 4])
            offset += 4
            wants_unicast |= bool(qclass & cls.CACHE_FLUSH)
            if (qclass & 0x7FFF) in (cls.CLASS_IN, cls.TYPE_ANY):
                result.append((name, qtype))
        return transaction_id, wants_unicast, result

    @classmethod
    def _record(cls, name, record_type, data, flush=False, ttl=None):
        record_class = cls.CLASS_IN | (cls.CACHE_FLUSH if flush else 0)
        return (cls._encode_name(name)
                + struct.pack(">HHIH", record_type, record_class,
                              cls.MDNS_TTL if ttl is None else ttl, len(data))
                + data)

    @staticmethod
    def _txt_data(values):
        output = bytearray()
        for value in values:
            raw = value.encode("utf-8")
            if len(raw) > 255:
                raise ValueError("DNS TXT entry exceeds 255 bytes")
            output.append(len(raw))
            output.extend(raw)
        return bytes(output or b"\x00")

    def _instance_name(self, service_type):
        label = self.model_name.encode("utf-8")[:63].decode("utf-8", errors="ignore")
        return "%s.%s" % (label, service_type)

    def build_announcement(self, transaction_id=0, enumerate_services=False,
                           goodbye=False):
        """Build a complete DNS-SD response for the active persona."""
        answers = []
        additionals = []
        ttl = 0 if goodbye else self.MDNS_TTL
        for service_type, port, txt in self.services:
            instance = self._instance_name(service_type)
            if enumerate_services:
                answers.append(self._record(
                    self.SERVICE_ENUMERATION, self.TYPE_PTR,
                    self._encode_name(service_type), ttl=ttl))
            answers.append(self._record(service_type, self.TYPE_PTR,
                                        self._encode_name(instance), ttl=ttl))
            srv = struct.pack(">HHH", 0, 0, port) + self._encode_name(self.fqdn)
            additionals.append(self._record(instance, self.TYPE_SRV, srv,
                                            flush=True, ttl=ttl))
            additionals.append(self._record(instance, self.TYPE_TXT,
                                            self._txt_data(txt), flush=True,
                                            ttl=ttl))
        additionals.append(self._record(self.fqdn, self.TYPE_A,
                                        socket.inet_aton(self.address),
                                        flush=True, ttl=ttl))
        return (struct.pack(">HHHHHH", transaction_id, 0x8400, 0,
                            len(answers), 0, len(additionals))
                + b"".join(answers) + b"".join(additionals))

    def response_for_query(self, packet):
        """Return ``(response, unicast_requested)`` for a relevant DNS query."""
        transaction_id, wants_unicast, questions = self._question_names(packet)
        known = {self.fqdn.lower(), self.SERVICE_ENUMERATION}
        for service_type, _port, _txt in self.services:
            known.add(service_type.lower())
            known.add(self._instance_name(service_type).lower())
        relevant = [(name, qtype) for name, qtype in questions
                    if name in known and qtype in (self.TYPE_A, self.TYPE_PTR,
                                                   self.TYPE_TXT, self.TYPE_SRV,
                                                   self.TYPE_ANY)]
        if not relevant:
            return None
        enumerate_services = any(name == self.SERVICE_ENUMERATION
                                 for name, _qtype in relevant)
        return (self.build_announcement(transaction_id=transaction_id,
                                        enumerate_services=enumerate_services),
                wants_unicast)

    def start(self):
        if self._is_running:
            return
        if ipaddress.ip_address(self.address).is_loopback:
            print("[GatewayDiscoveryResponder] DNS-SD suppressed for loopback binding")
            return
        try:
            self._udp_sock = create_multicast_listener(self.MDNS_PORT,
                                                       self.MDNS_GROUP)
            self._udp_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            if self.address != "127.0.0.1":
                self._udp_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                          socket.inet_aton(self.address))
            self._is_running = True
            self._thread = threading.Thread(target=self._listen_loop, daemon=True,
                                            name="GatewayDiscoveryResponder")
            self._thread.start()
            self._udp_sock.sendto(self.build_announcement(),
                                  (self.MDNS_GROUP, self.MDNS_PORT))
            print("[GatewayDiscoveryResponder] %s DNS-SD active on UDP %d"
                  % (self.persona, self.MDNS_PORT))
        except OSError as exc:
            if self._udp_sock:
                self._udp_sock.close()
            self._udp_sock = None
            self._is_running = False
            print("[GatewayDiscoveryResponder] mDNS unavailable: %s" % exc)

    def stop(self):
        sock = self._udp_sock
        if self._is_running and sock:
            try:
                sock.sendto(self.build_announcement(goodbye=True),
                            (self.MDNS_GROUP, self.MDNS_PORT))
            except OSError:
                pass
        self._is_running = False
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        self._udp_sock = None

    def _listen_loop(self):
        while self._is_running and self._udp_sock:
            try:
                data, addr = self._udp_sock.recvfrom(9000)
                response = self.response_for_query(data)
                if not response:
                    continue
                payload, wants_unicast = response
                legacy_unicast = addr[1] != self.MDNS_PORT
                if not legacy_unicast:
                    payload = b"\x00\x00" + payload[2:]
                destination = addr if wants_unicast or legacy_unicast else (
                    self.MDNS_GROUP, self.MDNS_PORT)
                self._udp_sock.sendto(payload, destination)
            except (OSError, UnicodeDecodeError, ValueError):
                if not self._is_running:
                    break