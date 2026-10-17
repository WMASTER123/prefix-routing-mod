"""Complete HTTP-family capture for the bounded CPU observer, not a hook.

Call begin after rendering and before creating/consuming any generator. Group
IDs and internal IDs come from actual calls, never reconstructed from suffixes.
"""

from dataclasses import dataclass, field

from tools.terminal_observer_prototype import TerminalJournal, _identifier


@dataclass
class Group:
    expected: int
    children: set[str] = field(default_factory=set)
    submitted: bool = False


class EntryCapture:
    """One attempt, finite retention; partial capture cannot emit ENDED."""

    def __init__(self, journal: TerminalJournal, attempt_id, group_sizes):
        _identifier(attempt_id)
        if (
            not isinstance(group_sizes, tuple)
            or not group_sizes
            or len(group_sizes) > journal.max_children
            or any(type(size) is not int or size <= 0 for size in group_sizes)
        ):
            raise ValueError("complete bounded group sizes required")
        journal.begin(attempt_id, sum(group_sizes))
        self.journal = journal
        self.attempt_id = attempt_id
        self.group_sizes = group_sizes
        self.groups: dict[str, Group] = {}
        self.abandoned = False

    def register_group(self, external_id, actual_n):
        _identifier(external_id)
        if self.abandoned or len(self.groups) >= len(self.group_sizes):
            raise ValueError("entry closed or group count exceeded")
        if external_id in self.groups:
            raise ValueError("external ID already captured")
        expected = self.group_sizes[len(self.groups)]
        if type(actual_n) is not int or actual_n != expected:
            raise ValueError("rendered and actual sampling counts differ")
        self.groups[external_id] = Group(expected)

    def bind(self, external_id, internal_id):
        group = self.groups[external_id]
        if self.abandoned or group.submitted or len(group.children) >= group.expected:
            raise ValueError("group capture closed or full")
        self.journal.bind_child(self.attempt_id, internal_id)
        group.children.add(internal_id)

    def submitted(self, external_id):
        group = self.groups[external_id]
        if self.abandoned or group.submitted or len(group.children) != group.expected:
            raise ValueError("incomplete, abandoned or repeated group submission")
        group.submitted = True
        if (
            len(self.groups) == len(self.group_sizes)
            and all(item.submitted for item in self.groups.values())
            and not self.journal.seal(self.attempt_id)
        ):
            raise RuntimeError("observer unavailable; retain unresolved debt")

    def abandon(self):
        if not self.journal.families[self.attempt_id].sealed:
            self.journal.abandon(self.attempt_id)
            self.abandoned = True
