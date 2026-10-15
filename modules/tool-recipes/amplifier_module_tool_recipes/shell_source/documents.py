"""Original-source lexical ownership for shell frames and FIFO here documents.

The Bash CST does not represent multiple here documents on one header reliably.
This reader owns collection before CST parsing; it never scans converted DATA
for syntax.  A byte-width-preserving skeleton lets the CST establish ordinary
command roles without mistaking document contents for executable statements.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .binding import events_in, neutral_bytes
from .model import BoundTemplate, HereDocument, ShellSourceError, Span


@dataclass(frozen=True)
class LexicalContext:
    mode: str
    frame: int


@dataclass(frozen=True)
class DocumentOwnership:
    documents: tuple[HereDocument, ...]
    skeleton: bytes
    contexts: tuple[LexicalContext, ...]


@dataclass(frozen=True)
class _Pending:
    operator: Span
    word: Span
    identifier: Span
    delimiter: bytes
    quoted: bool
    strip_tabs: bool
    frame: int
    ordinal: int


_IDENTIFIER = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*\Z")
_WORD_END = b" \t\r\n;&|()<>"


class _Reader:
    def __init__(self, bound: BoundTemplate):
        self.bound = bound
        self.source = neutral_bytes(bound)
        self.skeleton = bytearray(self.source)
        self.contexts = [LexicalContext("ordinary", 0)] * len(self.source)
        self.documents: list[HereDocument] = []
        self.frame_serial = 0
        self.ordinal = 0

    def mark(self, start: int, end: int, mode: str, frame: int) -> None:
        self.contexts[start:end] = [LexicalContext(mode, frame)] * (end - start)

    def new_frame(self) -> int:
        self.frame_serial += 1
        return self.frame_serial

    def quoted(self, start: int, quote: int, frame: int) -> int:
        mode = "single" if quote == 39 else "double"
        index = start + 1
        self.mark(start, index, mode, frame)
        while index < len(self.source):
            character = self.source[index]
            if character == quote:
                self.mark(index, index + 1, mode, frame)
                return index + 1
            if quote == 34 and self.source.startswith(b"$(", index):
                if self.source.startswith(b"$((", index):
                    index = self.arithmetic(index, frame, dollar=True)
                else:
                    self.mark(index, index + 2, mode, frame)
                    index = self.script(index + 2, self.new_frame(), 41)
                continue
            if quote == 34 and character == 96:
                self.mark(index, index + 1, mode, frame)
                index = self.script(index + 1, self.new_frame(), 96)
                continue
            if quote == 34 and character == 92 and index + 1 < len(self.source):
                if self.source[index + 1] in b'$`"\\\n':
                    self.mark(index, index + 2, mode, frame)
                    index += 2
                    continue
            self.mark(index, index + 1, mode, frame)
            index += 1
        raise ShellSourceError("UNCLOSED_QUOTE", "original template has an unclosed quote", Span(start, index))

    def arithmetic(self, start: int, frame: int, *, dollar: bool) -> int:
        index = start + (3 if dollar else 2)
        depth = 1
        while index < len(self.source):
            if self.source.startswith(b"))", index) and depth == 1:
                index += 2
                self.mark(start, index, "arithmetic", frame)
                return index
            if self.source[index] == 40:
                depth += 1
            elif self.source[index] == 41:
                depth -= 1
            index += 1
        raise ShellSourceError("UNCLOSED_ARITHMETIC", "original arithmetic frame is not closed", Span(start, index))

    def opener(self, start: int, frame: int) -> tuple[_Pending, int]:
        index = start + 2
        strip_tabs = self.source[index:index + 1] == b"-"
        if strip_tabs:
            index += 1
        operator = Span(start, index)
        while self.source[index:index + 1] in (b" ", b"\t"):
            index += 1
        word_start = index
        quoted = self.source[index:index + 1] in (b"'", b'"')
        quote = self.source[index] if quoted else None
        if quote is not None:
            index += 1
        identifier_start = index
        while index < len(self.source) and (self.source[index] != quote if quoted else self.source[index] not in _WORD_END):
            index += 1
        identifier_end = index
        delimiter = self.source[identifier_start:identifier_end]
        if quoted:
            if index == len(self.source):
                raise ShellSourceError("UNCLOSED_DELIMITER", "original document delimiter quote is not closed", Span(word_start, index))
            index += 1
        if not _IDENTIFIER.fullmatch(delimiter):
            raise ShellSourceError("DOCUMENT_DELIMITER_DOMAIN", "document delimiter must be an authored ASCII identifier", Span(word_start, index))
        if index < len(self.source) and self.source[index] not in _WORD_END:
            raise ShellSourceError("DOCUMENT_DELIMITER_DOMAIN", "mixed delimiter quoting is not owned", Span(word_start, index + 1))
        if any(o.span.start < index and o.span.end > word_start for o in self.bound.occurrences):
            raise ShellSourceError("DATA_DELIMITER", "context data cannot own a document delimiter", Span(word_start, index))
        pending = _Pending(operator, Span(word_start, index), Span(identifier_start, identifier_end), delimiter, quoted, strip_tabs, frame, self.ordinal)
        self.ordinal += 1
        return pending, index

    def collect(self, index: int, pending: list[_Pending]) -> int:
        for document in pending:
            body_start = index
            while index <= len(self.source):
                line_end = self.source.find(b"\n", index)
                if line_end < 0:
                    line_end = len(self.source)
                line = self.source[index:line_end]
                leading = len(line) - len(line.lstrip(b"\t")) if document.strip_tabs else 0
                if line[leading:] == document.delimiter:
                    body = Span(body_start, index)
                    closer = Span(index + leading, line_end)
                    final_lf = Span(index - 1, index) if index > body_start and self.source[index - 1:index] == b"\n" else None
                    self.documents.append(HereDocument(document.frame, document.ordinal, document.delimiter.decode("ascii"), document.quoted, document.strip_tabs, document.identifier, body, closer, events_in(self.bound, body), final_lf))
                    self.mark(body_start, line_end, "heredoc", document.frame)
                    # The redirect still occupies one CST destination, and all
                    # document/closer bytes retain their original coordinates.
                    self.skeleton[document.operator.start:document.operator.end] = b"<" + b" " * (document.operator.end - document.operator.start - 1)
                    for position in range(body_start, line_end):
                        if self.skeleton[position] != 10:
                            self.skeleton[position] = 32
                    index = line_end + (line_end < len(self.source))
                    break
                if line_end == len(self.source):
                    raise ShellSourceError("UNCLOSED_DOCUMENT", "original document has no exact owned closer", document.word)
                index = line_end + 1
        pending.clear()
        return index

    def script(self, index: int, frame: int, stop: int | None = None) -> int:
        pending: list[_Pending] = []
        word_start = True
        cases: list[bool] = []  # True while reading a case pattern.
        while index < len(self.source):
            character = self.source[index]
            self.mark(index, index + 1, "ordinary", frame)
            if character == stop and not (stop == 41 and cases and cases[-1]):
                if pending:
                    raise ShellSourceError("DOCUMENT_FRAME", "document collection crosses its shell frame", pending[0].word)
                return index + 1
            if character == 92:
                self.mark(index, min(index + 2, len(self.source)), "ordinary", frame)
                index += 2
                word_start = False
                continue
            if character in (39, 34):
                index = self.quoted(index, character, frame)
                word_start = False
                continue
            if self.source.startswith(b"$((", index):
                index = self.arithmetic(index, frame, dollar=True)
                word_start = False
                continue
            if self.source.startswith(b"((", index):
                index = self.arithmetic(index, frame, dollar=False)
                word_start = False
                continue
            if self.source.startswith(b"$(", index) or self.source[index:index + 2] in (b"<(", b">("):
                self.mark(index, index + 2, "ordinary", frame)
                index = self.script(index + 2, self.new_frame(), 41)
                word_start = False
                continue
            if character == 96:
                index = self.script(index + 1, self.new_frame(), 96)
                word_start = False
                continue
            if character == 35 and word_start:
                end = self.source.find(b"\n", index)
                if end < 0:
                    end = len(self.source)
                self.mark(index, end, "comment", frame)
                index = end
                continue
            if self.source.startswith(b"<<<", index):
                self.mark(index, index + 3, "ordinary", frame)
                index += 3
                word_start = True
                continue
            if self.source.startswith(b"<<", index):
                document, end = self.opener(index, frame)
                pending.append(document)
                self.mark(index, end, "document_header", frame)
                index = end
                word_start = True
                continue
            if character == 10:
                index = self.collect(index + 1, pending) if pending else index + 1
                word_start = True
                continue
            if character == 40:
                index = self.script(index + 1, self.new_frame(), 41)
                word_start = True
                continue
            if character == 41 and cases and cases[-1]:
                cases[-1] = False
            if self.source.startswith(b";;", index) and cases:
                cases[-1] = True
            if word_start and (65 <= character <= 90 or 97 <= character <= 122 or character == 95):
                end = index + 1
                while end < len(self.source) and (65 <= self.source[end] <= 90 or 97 <= self.source[end] <= 122 or 48 <= self.source[end] <= 57 or self.source[end] == 95):
                    end += 1
                token = self.source[index:end]
                if end == len(self.source) or self.source[end] in _WORD_END:
                    if token == b"case":
                        cases.append(True)
                    elif token == b"esac" and cases:
                        cases.pop()
                self.mark(index, end, "ordinary", frame)
                index = end
                word_start = False
                continue
            word_start = character in _WORD_END
            index += 1
        if pending or stop is not None:
            raise ShellSourceError("UNCLOSED_FRAME", "original shell frame or document is not closed", Span(index, index))
        return index


def collect_documents(bound: BoundTemplate) -> DocumentOwnership:
    reader = _Reader(bound)
    reader.script(0, 0)
    return DocumentOwnership(tuple(sorted(reader.documents, key=lambda d: d.ordinal)), bytes(reader.skeleton), tuple(reader.contexts))
