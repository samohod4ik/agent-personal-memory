"""Exact SequenceMatcher blocks with a linear longest-match search for large ranges."""
from array import array
from difflib import Match, SequenceMatcher
from .resources import available_memory


class ExactSequenceMatcher(SequenceMatcher):
    """Preserve difflib tie-breaking for strings without junk or autojunk."""

    def __init__(self, isjunk=None, a='', b='', autojunk=False, *, threshold=1_000_000):
        if isjunk is not None or autojunk:
            raise ValueError('exact matcher requires no junk and autojunk disabled')
        # difflib indexes all positions in b during construction, before
        # find_longest_match has a chance to check the automaton budget.
        if len(b) > 10_000 and len(b) * 128 > available_memory():
            raise MemoryError('insufficient memory for exact deletion')
        self.threshold = threshold
        super().__init__(isjunk, a, b, autojunk=False)

    def find_longest_match(self, alo=0, ahi=None, blo=0, bhi=None):
        if ahi is None:
            ahi = len(self.a)
        if bhi is None:
            bhi = len(self.b)
        if alo >= ahi or blo >= bhi:
            return Match(alo, blo, 0)
        # A full occurrence is necessarily the longest possible block, and
        # find() gives the same earliest-position tie-break as difflib.
        a = self.a[alo:ahi]
        b = self.b[blo:bhi]
        if len(a) <= len(b):
            pos = b.find(a)
            if pos >= 0:
                return Match(alo, blo + pos, len(a))
        else:
            pos = a.find(b)
            if pos >= 0:
                return Match(alo + pos, blo, len(b))
        if len(a) * len(b) <= self.threshold:
            return super().find_longest_match(alo, ahi, blo, bhi)

        # Suffix automaton of b. Each state tracks the earliest end position
        # among all its occurrences; this supplies difflib's earliest-b tie.
        # At most twice as many states as characters are created. Account
        # conservatively for Python dicts, transitions, arrays and sorting.
        if len(b) * 2048 > available_memory():
            raise MemoryError('insufficient memory for exact deletion')
        transitions = [{}]
        links = array('i', [-1])
        lengths = array('i', [0])
        infinity = len(b) + 1
        earliest = array('i', [infinity])
        last = 0
        for index, char in enumerate(b):
            current = len(transitions)
            transitions.append({})
            lengths.append(lengths[last] + 1)
            links.append(0)
            earliest.append(index)
            p = last
            while p >= 0 and char not in transitions[p]:
                transitions[p][char] = current
                p = links[p]
            if p >= 0:
                q = transitions[p][char]
                if lengths[p] + 1 == lengths[q]:
                    links[current] = q
                else:
                    clone = len(transitions)
                    transitions.append(transitions[q].copy())
                    lengths.append(lengths[p] + 1)
                    links.append(links[q])
                    earliest.append(infinity)
                    while p >= 0 and transitions[p].get(char) == q:
                        transitions[p][char] = clone
                        p = links[p]
                    links[q] = links[current] = clone
            last = current
        for state in sorted(range(1, len(transitions)), key=lengths.__getitem__, reverse=True):
            parent = links[state]
            if earliest[state] < earliest[parent]:
                earliest[parent] = earliest[state]

        state = matched = 0
        best_len = 0
        best_a = alo
        best_b = blo
        for index, char in enumerate(a):
            while state and char not in transitions[state]:
                state = links[state]
                matched = min(matched, lengths[state])
            if char in transitions[state]:
                state = transitions[state][char]
                matched += 1
            else:
                state = matched = 0
                continue
            a_start = alo + index - matched + 1
            b_start = blo + earliest[state] - matched + 1
            if matched > best_len or (matched == best_len and (a_start, b_start) < (best_a, best_b)):
                best_len, best_a, best_b = matched, a_start, b_start
        return Match(best_a, best_b, best_len)
