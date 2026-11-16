"""Extended grapheme cluster segmentation pinned to UAX #29 / Unicode 14.0.0.

Property data comes from the frozen ``_ucd14_data`` tables, not from the
running interpreter's unicodedata version, so segmentation results are
identical across runtimes. The streaming splitter retains only the single
unfinished trailing cluster plus its state; a cluster is never released
while a future codepoint could still extend it (GB3/GB9/GB9a/GB9b/
GB11/GB12/GB13). The one-shot helper is a thin wrapper over the same
machine, so both paths necessarily agree.
"""
from bisect import bisect_right

from ._ucd14_data import GCB_VALUES, _GCB_RANGES, _EP_RANGES, UNIDATA_VERSION

SEGMENTATION_VERSION = f"UAX29-extended-grapheme-cluster/{UNIDATA_VERSION}"
_CR, _LF = GCB_VALUES.index("CR"), GCB_VALUES.index("LF")
_CONTROL = GCB_VALUES.index("Control")
_EXTEND, _ZWJ = GCB_VALUES.index("Extend"), GCB_VALUES.index("ZWJ")
_SPACING, _PREPEND = GCB_VALUES.index("SpacingMark"), GCB_VALUES.index("Prepend")
_RI = GCB_VALUES.index("Regional_Indicator")
_L, _V, _T, _LV, _LVT = (GCB_VALUES.index(k) for k in ("L", "V", "T", "LV", "LVT"))
_OTHER = -1
_GCB_STARTS = [r[0] for r in _GCB_RANGES]
_EP_STARTS = [r[0] for r in _EP_RANGES]


def gcb(codepoint):
    """Grapheme_Cluster_Break property value name for a single codepoint."""
    i = bisect_right(_GCB_STARTS, codepoint) - 1
    if i >= 0 and codepoint <= _GCB_RANGES[i][1]:
        return GCB_VALUES[_GCB_RANGES[i][2]]
    return "Other"


def gcb_index(codepoint):
    i = bisect_right(_GCB_STARTS, codepoint) - 1
    if i >= 0 and codepoint <= _GCB_RANGES[i][1]:
        return _GCB_RANGES[i][2]
    return _OTHER  # implicit GCB value: Other


def is_extended_pictographic(codepoint):
    i = bisect_right(_EP_STARTS, codepoint) - 1
    return i >= 0 and codepoint <= _EP_RANGES[i][1]


class _ClusterState:
    """State of the single unfinished trailing cluster (UAX #29 rules)."""

    __slots__ = ("ri_count", "last_ep_clean", "trailing_zwj", "only_prepend", "length")

    def __init__(self):
        self.ri_count = 0
        # last_ep_clean: true since the most recent Extended_Pictographic
        # only Extend/ZWJ codepoints have appeared (or the cluster just
        # started on an EP). This models "EP Extend*" including the EP that
        # GB11 itself pulls into the cluster.
        self.last_ep_clean = False
        # trailing_zwj: the immediately preceding codepoint was a ZWJ whose
        # prefix still satisfies GB11.
        self.trailing_zwj = False
        self.only_prepend = True
        self.length = 0

    def add_first(self, prop, ep):
        self.ri_count = 1 if prop == _RI else 0
        self.last_ep_clean = ep
        self.trailing_zwj = False
        self.only_prepend = prop == _PREPEND
        self.length = 1

    def add_next(self, prop, ep):
        if prop == _RI:
            self.ri_count += 1
        else:
            self.ri_count = 0
        if prop == _ZWJ:
            self.trailing_zwj = self.last_ep_clean
        elif prop == _EXTEND:
            self.trailing_zwj = False
            # last_ep_clean unchanged: Extend is legal inside EP Extend*.
        else:
            self.trailing_zwj = False
            # A fresh EP restarts the GB11 prefix (this includes an EP that
            # GB11 itself just pulled in); any other base breaks it.
            self.last_ep_clean = ep
        if prop != _PREPEND:
            self.only_prepend = False
        self.length += 1


def _break_before(prev_prop, cur_prop, cur_ep, state):
    # GB3: CR x LF
    if prev_prop == _CR and cur_prop == _LF:
        return False
    # GB4 / GB5
    if prev_prop in (_CONTROL, _CR, _LF) or cur_prop in (_CONTROL, _CR, _LF):
        return True
    # GB6
    if prev_prop == _L and cur_prop in (_L, _V, _LV, _LVT):
        return False
    # GB7
    if prev_prop in (_LV, _V) and cur_prop in (_V, _T):
        return False
    # GB8
    if prev_prop in (_LVT, _T) and cur_prop == _T:
        return False
    # GB9
    if cur_prop in (_EXTEND, _ZWJ):
        return False
    # GB9a
    if cur_prop == _SPACING:
        return False
    # GB9b (Unicode 14: Prepend x any)
    if state.only_prepend:
        return False
    # GB11: EP Extend* ZWJ x EP
    if state.trailing_zwj and cur_ep:
        return False
    # GB12 / GB13
    if cur_prop == _RI and state.ri_count % 2 == 1:
        return False
    # GB999
    return True


class StreamingGraphemeSplitter:
    """Incremental splitter robust to cuts at any codepoint boundary.

    Chunks may split a base character from its combining marks, an emoji
    ZWJ sequence, a regional indicator pair, a CR/LF pair or even a UTF-16
    surrogate pair: only the unfinished trailing cluster is retained and a
    cluster is released once its boundaries can no longer change.
    """

    def __init__(self):
        self._held = ""
        self._state = None
        self._prev_prop = None

    def add(self, chunk):
        if not isinstance(chunk, str):
            raise TypeError("text chunks must be strings")
        emitted = []
        for ch in chunk:
            cp = ord(ch)
            prop = gcb_index(cp)
            ep = is_extended_pictographic(cp)
            if self._state is None:
                self._state = _ClusterState()
                self._state.add_first(prop, ep)
            elif _break_before(self._prev_prop, prop, ep, self._state):
                emitted.append(self._held)
                self._held = ""
                self._state = _ClusterState()
                self._state.add_first(prop, ep)
            else:
                self._state.add_next(prop, ep)
            self._held += ch
            self._prev_prop = prop
        return emitted

    def finish(self):
        if self._state is None:
            return []
        tail = self._held
        self._held, self._state, self._prev_prop = "", None, None
        return [tail]

    @property
    def pending_codepoints(self):
        return len(self._held)


def grapheme_clusters(text):
    """One-shot segmentation, produced by the same streaming state machine."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    splitter = StreamingGraphemeSplitter()
    return [*splitter.add(text), *splitter.finish()]


def grapheme_boundaries(text):
    """Codepoint indices of every cluster boundary, including 0 and len."""
    boundaries, total = [0], 0
    for cluster in grapheme_clusters(text):
        total += len(cluster)
        boundaries.append(total)
    return boundaries
