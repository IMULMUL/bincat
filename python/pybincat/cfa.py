
import ConfigParser
from collections import defaultdict
import re
from pybincat.tools import parsers
from pybincat import PyBinCATException
import tempfile
import functools


def reg_len(regname):
    """
    Return register length in bits
    """
    return {
        "eax": 32, "ebx": 32, "ecx": 32, "edx": 32,
        "esi": 32, "edi": 32, "esp": 32, "ebp": 32,
        "ax": 16, "bx": 16, "cx": 16, "dx": 16,
        "si": 16, "di": 16, "sp": 16, "bp": 16,
        "cs": 16, "ds": 16, "es": 16, "ss": 16, "fs": 16, "gs": 16,
        "iopl": 2,
        "cf": 1, "pf": 1, "af": 1, "zf": 1, "sf": 1, "tf": 1, "if": 1,
        "df": 1, "of": 1, "nt": 1, "rf": 1, "vm": 1, "ac": 1, "vif": 1,
        "vip": 1, "id": 1,
    }[regname]

#: maps short region names to pretty names
PRETTY_REGIONS = {'g': 'global', 's': 'stack', 'h': 'heap',
                  'b': 'bottom', 't': 'top'} # used for pointers only


class PyBinCATParseError(PyBinCATException):
    pass


class CFA(object):
    """
    Holds State for each defined node_id.
    Several node_ids may share the same address (ex. loops, partitions)
    """

    def __init__(self, states, edges, nodes):
        #: Value (address) -> [node_id]. Nodes marked "final" come first.
        self.states = states
        #: node_id (string) -> list of node_id (string)
        self.edges = edges
        #: node_id (string) -> State
        self.nodes = nodes
        self.logs = None

    @classmethod
    def parse(cls, filename, logs=None):

        states = defaultdict(list)
        edges = defaultdict(list)
        nodes = {}

        config = ConfigParser.RawConfigParser()
        try:
            config.read(filename)
        except ConfigParser.ParsingError:
            return None
        if len(config.sections()) == 0:
            return None

        for section in config.sections():
            if section == 'edges':
                for edgename, edge in config.items(section):
                    src, dst = edge.split(' -> ')
                    edges[src].append(dst)
                continue
            elif section.startswith('node = '):
                node_id = section[7:]
                state = State.parse(node_id, dict(config.items(section)))
                address = state.address
                if state.final:
                    states[address].insert(0, state.node_id)
                else:
                    states[address].append(state.node_id)
                nodes[state.node_id] = state
                continue
            raise PyBinCATException("Cannot parse section name (%r)" % section)

        cfa = cls(states, edges, nodes)
        if logs:
            cfa.logs = open(logs).read()
        return cfa

    @classmethod
    def from_analysis(cls, initfname):
        """
        Runs analysis from provided init file
        """
        outfile = tempfile.NamedTemporaryFile()
        logfile = tempfile.NamedTemporaryFile()

        return cls.from_filenames(initfname, outfile.name, logfile.name)

    @classmethod
    def from_state(cls, state):
        """
        Runs analysis.
        """
        initfile = tempfile.NamedTemporaryFile()
        initfile.write(str(state))
        initfile.close()

        return cls.from_analysis(initfile.name)

    @classmethod
    def from_filenames(cls, initfname, outfname, logfname):
        """
        Runs analysis, using provided filenames.

        :param initfname: string, path to init file
        :param outfname: string, path to output file
        :param logfname: string, path to log file
        """
        from pybincat import mlbincat
        mlbincat.process(initfname, outfname, logfname)
        return cls.parse(outfname, logs=logfname)

    def _toValue(self, eip, region="g"):
        if type(eip) in [int, long]:
            addr = Value(region, eip, 0)
        elif type(eip) is Value:
            addr = eip
        elif type(eip) is str:
            addr = Value(region, int(eip), 0)
        # else:
        #     logging.error(
        #         "Invalid address %s (type %s) in AnalyzerState._toValue",
        #         eip, type(eip))
        #     addr = None
        return addr

    def __getitem__(self, node_id):
        """
        Returns State at provided node_id if it exists, else None.
        """
        return self.nodes.get(node_id, None)

    def node_id_from_addr(self, addr):
        addr = self._toValue(addr)
        return self.states[addr]

    def next_states(self, node_id):
        """
        Returns a list of State
        """
        return [self[n] for n in self.edges[node_id]]


class State(object):
    """
    Contains memory & registers status

    bincat output format examples:
    reg [eax] = S0xfff488!0
    111  222    33333333333

    mem[G0x1234, G0x1236] = G0x20, G0x0
    111 2222222222222222    33333  3333 <-- list of 2 valtaint

    mem[G0x24*32] = G0b????1111!0b????0000
    111 22222222    3333333333333333333333 <-- list of 1 valtaint

    1: src region (overridden with src region contained in address for memory)
    2: address
    3: dst region, value, taint (valtaint)

    example valtaints: G0x1234 G0x12!0xF0 S0x12!ALL
    """
    __slots__ = ['re_region_addr', 're_valtaint', 'address', 'node_id',
                 '_regaddrs', 'final', 'statements', 'bytes', 'tainted',
                 '_outputkv']
    #: split src region + address (left of '=')
    re_region_addr = re.compile("(?P<region>reg|mem)\s*\[(?P<addr>[^]]+)\]")
    #: split value

    re_valtaint = re.compile(
        "(?P<memreg>[a-zA-Z])(?P<value>0[xb][0-9a-fA-F_?]+)(?P<taint>!\s+)?")

    def __init__(self, node_id, address=None, lazy_init=None):
        self.address = address
        self.node_id = node_id
        #: Value -> [Value]. Either 1 value, or a list of 1-byte Values.
        self._regaddrs = {}
        self.final = False
        self.statements = ""
        self.bytes = ""
        self.tainted = False

    @property
    def regaddrs(self):
        if self._regaddrs is None:
            try:
                self.parse_regaddrs()
            except Exception as e:
                import traceback
                traceback.print_exc(e)
                raise PyBinCATException(
                    "Cannot parse regaddrs at address %s" % self.address)
        return self._regaddrs

    @classmethod
    def parse(cls, node_id, outputkv):
        """
        :param outputkv: list of (key, value) tuples for each property set by
            the analyzer at this EIP
        """

        new_state = State(node_id)
        addr = outputkv.pop("address")
        m = cls.re_valtaint.match(addr)
        new_state.address = Value(m.group("memreg"), int(m.group("value"), 0), 0)
        new_state.final = outputkv.pop("final", None) == "true"
        new_state.statements = outputkv.pop("statements", "")
        new_state.bytes = outputkv.pop("bytes", "")
        new_state.tainted = outputkv.pop("tainted", "False") == "true"
        new_state._outputkv = outputkv
        new_state._regaddrs = None
        return new_state

    def parse_regaddrs(self):
        self._regaddrs = {}
        for k, v in self._outputkv.iteritems():
            m = self.re_region_addr.match(k)
            if not m:
                raise PyBinCATException("Parsing error (key=%r)" % (k,))
            region = m.group("region")
            addr = m.group("addr")
            if region == "mem":
                # use memreg as region instead of 'mem'
                # ex. "s0xabcd, s0xabce" "g0x24*32"
                # region in ['s', 'g', 'h']
                if '*' in addr:
                    # single repeated value
                    regaddr, l = addr.split('*')
                    length = int(l)
                    m = self.re_valtaint.match(regaddr)
                    region, addr = m.group('memreg'), m.group('value')
                    v = ', '.join([v] * length)
                else:
                    regaddr1, regaddr2 = addr.split(', ')
                    m = self.re_valtaint.match(regaddr1)
                    region1, addr = m.group('memreg'), m.group('value')
                    m = self.re_valtaint.match(regaddr2)
                    region2, addr2 = m.group('memreg'), m.group('value')
                    assert region1 == region2
                    region = region1
                    length = (parsers.parse_val(addr2)[0] -
                              parsers.parse_val(addr)[0]) * 8
                    # XXX allow non-aligned access
            elif region == "reg":
                length = reg_len(addr)

            # build value
            concat_value = None
            prev_memreg = None
            #: offset to last saved address - used in case different memreg
            #: values are present
            saved_offset = 0
            regaddr = Value.parse(region, addr, '0', 0)
            for idx, val in enumerate(v.split(', ')):
                m = self.re_valtaint.match(val)
                if not m:
                    raise PyBinCATException("Parsing error (value=%r)" % (v,))
                memreg = m.group("memreg")
                strval = m.group("value")
                taint = m.group("taint")
                new_value = Value.parse(memreg, strval, taint, length)
                if prev_memreg == memreg or prev_memreg is None:
                    # concatenate
                    if concat_value is None:
                        concat_value = [new_value]
                    else:
                        concat_value.append(new_value)
                else:
                    if prev_memreg is not None:
                        # save previous value
                        self._regaddrs[regaddr+saved_offset] = concat_value
                        concat_value = None
                        saved_offset = idx
                    prev_memreg = memreg

            self._regaddrs[regaddr+saved_offset] = concat_value
        del(self._outputkv)

    def __getitem__(self, item):
        """
        Return list of Value
        """
        if type(item) is not Value:
            raise KeyError
        if item in self.regaddrs:
            return self.regaddrs[item]
        else:
            # looking for address in list of 1-byte Value
            for addr in self.regaddrs:
                if addr.region != item.region:
                    continue
                vlist = self.regaddrs[addr]
                v0 = vlist[0]
                if not v0.value < item.value:
                    continue
                if v0.value + len(vlist) >= item.value:
                    return self.regaddrs[item][item.value-v0.value:]
            raise IndexError

    def __setitem__(self, item, val):
        # XXX allow random access
        self.regaddrs[item] = val

    def __getattr__(self, attr):
        if attr.startswith('__'):  # avoid failure in copy.deepcopy()
            raise AttributeError(attr)
        try:
            return self.regaddrs[attr]
        except KeyError as e:
            raise AttributeError(attr)

    def __eq__(self, other):
        if set(self.regaddrs.keys()) != set(other.regaddrs.keys()):
            return False
        for regaddr in self.regaddrs.keys():
            if self.regaddrs[regaddr] != other.regaddrs[regaddr]:
                return False
        return True

    def list_modified_keys(self, other):
        """
        Returns a set of (region, name) for which value or tainting
        differ between self and other.
        """
        # List keys present in only one of the states
        sRA = set(self.regaddrs)
        oRA = set(other.regaddrs)
        results = sRA.symmetric_difference(oRA)
        # Check values
        for regaddr in sRA & oRA:
            if self[regaddr] != other[regaddr]:
                results.add(regaddr)
        return results

    def diff(self, other, pns="", pno="", parent=None):
        """
        :param pns: pretty name for self
        :param pno: pretty name for other
        """
        pns += str(self)
        pno += str(other)
        res = ["--- %s" % pns, "+++ %s" % pno]
        if parent:
            res.append("000 Parent %s" % str(parent))
        for regaddr in self.list_modified_keys(other):
            region = regaddr.region
            address = regaddr.value
            if regaddr.is_concrete() and isinstance(address, int):
                address = "%#08x" % address
            res.append("@@ %s %s @@" % (region, address))
            if (parent is not None) and (regaddr in parent.regaddrs):
                res.append("0 %s" % parent.regaddrs[regaddr])
            if regaddr not in self.regaddrs:
                res.append("+ %s" % other.regaddrs[regaddr])
            elif regaddr not in other.regaddrs:
                res.append("- %s" % self.regaddrs[regaddr])
            elif self.regaddrs[regaddr] != other.regaddrs[regaddr]:
                res.append("- %s" % self.regaddrs[regaddr])
                res.append("+ %s" % other.regaddrs[regaddr])
        return "\n".join(res)

    def __repr__(self):
        return "State at address %s" % self.address


@functools.total_ordering
class Value(object):
    __slots__ = ['vtop', 'vbot', 'taint', 'ttop', 'tbot', 'length', 'value', 'region']

    def __init__(self, region, value, length=None, vtop=0, vbot=0, taint=0,
                 ttop=0, tbot=0):
        self.region = region.lower()
        self.value = value
        if not length and region == 'reg':
            self.length = reg_len(value)
        else:
            self.length = length
        self.vtop = vtop
        self.vbot = vbot
        self.taint = taint
        self.ttop = ttop
        self.tbot = tbot

    @classmethod
    def parse(cls, region, s, t, length):
        value, vtop, vbot = parsers.parse_val(s)
        if t is None or t == "NONE":
            taint, ttop, tbot = (0, 0, 0)
        elif t == "ALL":
            taint, ttop, tbot = (2**length-1, 0, 0)
        else:
            taint, ttop, tbot = parsers.parse_val(t)
        return cls(region, value, length, vtop, vbot, taint, ttop, tbot)

    @property
    def prettyregion(self):
        return PRETTY_REGIONS.get(self.region, self.region)

    def __len__(self):
        return self.length

    def __repr__(self):
        return "Value(%s, %s ! %s)" % (
            self.region,
            self.__valuerepr__(),
            self.__taintrepr__())

    def __valuerepr__(self):
        return parsers.val2str(self.value, self.vtop, self.vbot, self.length)

    def __taintrepr__(self):
        return parsers.val2str(self.taint, self.ttop, self.tbot, self.length)

    def __hash__(self):
        return hash((type(self), self.region, self.value,
                     self.vtop, self.vbot, self.taint,
                     self.ttop, self.tbot))

    def __eq__(self, other):
        return (self.region == other.region and
                self.value == other.value and self.taint == other.taint and
                self.vtop == other.vtop and self.ttop == other.ttop and
                self.vbot == other.vbot and self.tbot == other.tbot)

    def __ne__(self, other):
        return not (self == other)

    def __lt__(self, other):
        return (self.region, self.value) < (other.region, other.value)

    def __add__(self, other):
        other = getattr(other, "value", other)
        if other == 0:
            # special case, useful when the value is a register name
            return self
        return self.__class__(self.region, self.value+other,
                              self.vtop, self.vbot, self.taint,
                              self.ttop, self.tbot)

    def __and__(self, other):
        """ concatenation """
        if self.region != other.region:
            raise TypeError(
                "Concatenation can only be performed between Value objects "
                "having the same region. %s != %s", self.region, other.region)
        return self.__class__(
            region=self.region,
            value=(self.value << other.length) + other.value,
            length=self.length+other.length,
            vtop=(self.vtop << other.length) + other.vtop,
            vbot=(self.vbot << other.length) + other.vbot,
            taint=(self.taint << other.length) + other.taint,
            ttop=(self.ttop << other.length) + other.ttop,
            tbot=(self.tbot << other.length) + other.tbot,
            )

    def __sub__(self, other):
        other = getattr(other, "value", other)
        newvalue = self.value-other
        # XXX clear value where top or bottom mask is not null
        # XXX complete implementation
        return self.__class__(self.region, newvalue,
                              self.vtop, self.vbot, self.taint,
                              self.ttop, self.tbot)

    def __iand__(self, other):
        other = getattr(other, "value", other)
        self.value &= other
        return self

    def __ior__(self, other):
        other = getattr(other, "value", other)
        self.value |= other
        return self

    def is_concrete(self):
        return self.vtop == 0 and self.vbot == 0

    def is_tainted(self):
        return (self.taint != 0 or
                self.ttop != 0 or
                self.tbot != 0)
