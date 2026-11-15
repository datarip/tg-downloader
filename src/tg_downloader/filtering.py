"""Pure, testable file-list filtering for the Files tab.

Telethon-free (stdlib only) by the same rule as ``filters.py``/``models.py``: the
predicate logic lives here so it can be unit-tested without a Telegram session,
while ``app.py`` keeps only the thin Tk widgets that feed it primitives.

The Status filter buckets a raw ``FileItem.status`` string into one of a small,
fixed set of categories via :func:`status_category`. That matches the way the
rest of the app already classifies statuses — by prefix/exact text
(``_row_tag``, the run-finish tail) rather than by importing constants — so this
module needs nothing from ``app.py`` and stays importable on its own.

A throttle (rate-limited / speed-capped) and a still-selected error are all
**work that hasn't finished downloading**, so they read as ``"queued"`` here and
in the Status column — not as their own categories. The *reason* a row is
stalled (the error/retry detail) is surfaced separately by :func:`status_note`,
which the GUI shows in its own Error column. Keeping these two in lockstep is
what lets the Status column and the Status filter agree.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

MB = 1024 * 1024


def _as_terms(value: str | Sequence[str]) -> tuple[str, ...]:
    """Normalise a text predicate into a tuple of non-empty search terms.

    A bare string is treated as a single term (so the historic single-keyword
    form still works); an iterable becomes one term per element. Blank entries
    are dropped and surrounding whitespace stripped.

    >>> _as_terms("report")
    ('report',)
    >>> _as_terms(["cats", " ", "dogs "])
    ('cats', 'dogs')
    >>> _as_terms("")
    ()
    """
    if not value:
        return ()
    items = [value] if isinstance(value, str) else value
    return tuple(t for t in (s.strip() for s in items) if t)


#: Status categories the Status filter exposes, in display (lifecycle) order.
#: Every real status maps onto one of these — a ticked, not-yet-finished file
#: (fresh, throttled, retrying, or errored-but-ticked) is ``"queued"``; an
#: unticked file the user set aside is ``"skipped"``. Only a genuinely unknown
#: status falls through to the unlisted ``"other"`` bucket, hidden whenever any
#: named category is actively selected.
STATUS_CHOICES = (
    "downloading",
    "queued",
    "incomplete",
    "paused",
    "stopped",
    "completed",
    "moved",
    "skipped",
)

#: Human labels for the checkboxes, keyed by category.
STATUS_LABELS = {
    "downloading": "Downloading",
    "queued": "Queued",
    "incomplete": "Incomplete",
    "paused": "Paused",
    "stopped": "Stopped",
    "completed": "Completed",
    "moved": "Moved",
    "skipped": "Skipped",
}


def status_category(status: str, selected: bool) -> str:
    """Bucket a raw status string (+ tick state) into one filterable category.

    A ticked file with no status yet is **queued** — it's on the list and will
    download but hasn't started. A throttled (rate-limited / speed-capped) file
    that's still ticked is **paused**: its download is parked on the shared
    rate-limit cooldown, so it reads like any other paused download (and flips
    back to ``downloading`` when the wait lapses). A mid-retry or still-ticked
    error is **queued**: it stays on the list and re-downloads, so it isn't a
    category of its own — the reason is carried by :func:`status_note`. An
    **unticked** file the user set aside — whether it had no status or a
    throttle/error when skipped — is **skipped**. The remaining lifecycle states
    (``incomplete``/``paused``/``stopped``/``moved``/``completed``) each get their
    own category so every status is filterable; only a truly unknown status falls
    through to ``"other"``.

    >>> status_category("downloading… ", True)
    'downloading'
    >>> status_category("", True)
    'queued'
    >>> status_category("", False)
    'skipped'
    >>> status_category("on disk", False)
    'completed'
    >>> status_category("incomplete", True)
    'incomplete'
    >>> status_category("stopped", True)
    'stopped'
    >>> status_category("moved", False)
    'moved'
    >>> status_category("speed capped", True)
    'paused'
    >>> status_category("speed capped", False)
    'skipped'
    >>> status_category("no connection", True)
    'paused'
    >>> status_category("error: not found", True)
    'queued'
    >>> status_category("error: not found", False)
    'skipped'
    """
    s = status.strip().lower()
    if s.startswith("downloading"):
        return "downloading"
    if s in ("completed", "on disk"):
        return "completed"
    if s == "incomplete":
        return "incomplete"
    if s == "paused":
        return "paused"
    if s == "stopped":
        return "stopped"
    if s == "moved":
        return "moved"
    # A throttle or a lost connection is a *wait*: the download is parked on the
    # shared cooldown / offline gate, so a ticked such file reads "paused" (and
    # returns to "downloading" once the wait lapses). Transient retries and
    # still-ticked errors are unfinished work that re-downloads, so they read
    # "queued"; their detail lives in the Error column via status_note. Unticked,
    # either is a file the user chose to set aside: skipped.
    if s.startswith(("rate limited", "speed capped", "no connection")):
        return "paused" if selected else "skipped"
    if s.startswith(("retry", "error")):
        return "queued" if selected else "skipped"
    if not s:
        return "queued" if selected else "skipped"
    return "other"


def status_note(status: str) -> str:
    """The Error-column detail for a row: why it stalled, or ``""`` if it didn't.

    Genuine errors and in-flight retries surface their reason; a throttle is not
    an error and a healthy row has nothing to note, so both return ``""``. The
    ``"error: "`` prefix is stripped — the column header already says "Error".

    >>> status_note("error: not found")
    'not found'
    >>> status_note("retry: timeout")
    'retry: timeout'
    >>> status_note("speed capped")
    ''
    >>> status_note("no connection")
    ''
    >>> status_note("downloading… 40%")
    ''
    >>> status_note("")
    ''
    """
    s = status.strip()
    low = s.lower()
    if low.startswith("error"):
        _, _, rest = s.partition(":")
        return rest.strip() or "error"
    if low.startswith("retry"):
        return s
    return ""


@dataclass
class FileFilter:
    """Per-column predicates for the Files tab; blank fields don't constrain.

    Text predicates are case-insensitive **keyword lists** (the Files-tab pill
    inputs). Matching is **OR**: a ``*_contains`` list passes a row that contains
    *any* one of its keywords; a ``*_excludes`` list hides a row that contains
    *any* one of its keywords. A bare string is accepted as a single keyword, so
    the historic single-term form keeps working. Size bounds are in **MB**
    (converted to bytes here). ``status_exclude`` is a *negative* set of
    :data:`STATUS_CHOICES` — the categories to **hide** (every box ticked → empty
    → hide nothing). This mirrors the all-ticked-by-default header checkboxes:
    unticking a box adds its category here. An all-default filter matches
    everything (:meth:`is_empty`).
    """

    name_contains: str | Sequence[str] = ()
    name_excludes: str | Sequence[str] = ()
    channel_contains: str | Sequence[str] = ()
    channel_excludes: str | Sequence[str] = ()
    size_min_mb: float | None = None  # "Size Above (MB)"
    size_max_mb: float | None = None  # "Size Below (MB)"
    progress_min: float | None = None  # "Progress Above (%)"
    progress_max: float | None = None  # "Progress Below (%)"
    status_exclude: frozenset[str] = field(default_factory=frozenset)  # hide these

    def __post_init__(self) -> None:
        # Coerce every text predicate to a tuple of terms so the rest of the
        # class can assume a uniform shape whether it was handed a pill list or a
        # single legacy string.
        self.name_contains = _as_terms(self.name_contains)
        self.name_excludes = _as_terms(self.name_excludes)
        self.channel_contains = _as_terms(self.channel_contains)
        self.channel_excludes = _as_terms(self.channel_excludes)

    def is_empty(self) -> bool:
        """True when nothing is constrained, so the whole list shows.

        >>> FileFilter().is_empty()
        True
        >>> FileFilter(name_contains="x").is_empty()
        False
        >>> FileFilter(name_contains=["a", "b"]).is_empty()
        False
        >>> FileFilter(status_exclude=frozenset({"completed"})).is_empty()
        False
        """
        return not (
            self.name_contains
            or self.name_excludes
            or self.channel_contains
            or self.channel_excludes
            or self.size_min_mb is not None
            or self.size_max_mb is not None
            or self.progress_min is not None
            or self.progress_max is not None
            or self.status_exclude
        )

    def matches(
        self,
        *,
        name: str,
        channel: str,
        size: int,
        progress: float,
        category: str,
    ) -> bool:
        """True if a row with these values passes every active predicate.

        ``progress`` is the 0–100 percent actually shown (100 for a completed
        file, 0 for one that never started); ``category`` is a
        :func:`status_category` result.

        >>> f = FileFilter(name_contains="report", size_min_mb=1)
        >>> f.matches(name="Q3-report.zip", channel="Grp", size=5 * MB,
        ...           progress=0, category="queued")
        True
        >>> f.matches(name="notes.zip", channel="Grp", size=5 * MB,
        ...           progress=0, category="queued")
        False
        >>> f.matches(name="report.zip", channel="Grp", size=10,
        ...           progress=0, category="queued")
        False

        Multiple keywords are OR: a ``*_contains`` list passes on *any* match, a
        ``*_excludes`` list hides on *any* match.

        >>> g = FileFilter(name_contains=["invoice", "report"])
        >>> [g.matches(name=n, channel="G", size=1, progress=0, category="queued")
        ...  for n in ("report.zip", "invoice.pdf", "photo.jpg")]
        [True, True, False]
        >>> h = FileFilter(name_excludes=["tmp", "draft"])
        >>> [h.matches(name=n, channel="G", size=1, progress=0, category="queued")
        ...  for n in ("final.zip", "draft.zip", "tmp.zip")]
        [True, False, False]
        >>> FileFilter(status_exclude=frozenset({"completed"})).matches(
        ...     name="x", channel="G", size=1, progress=100, category="completed")
        False
        """
        n, c = name.lower(), channel.lower()
        if self.name_contains and not any(t.lower() in n for t in self.name_contains):
            return False
        if any(t.lower() in n for t in self.name_excludes):
            return False
        if self.channel_contains and not any(t.lower() in c for t in self.channel_contains):
            return False
        if any(t.lower() in c for t in self.channel_excludes):
            return False
        if self.size_min_mb is not None and size < self.size_min_mb * MB:
            return False
        if self.size_max_mb is not None and size > self.size_max_mb * MB:
            return False
        if self.progress_min is not None and progress < self.progress_min:
            return False
        if self.progress_max is not None and progress > self.progress_max:
            return False
        if category in self.status_exclude:
            return False
        return True
