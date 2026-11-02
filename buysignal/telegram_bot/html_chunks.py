"""Split generated Telegram HTML without cutting entities or leaving open tags."""

from html import escape
from html.parser import HTMLParser


def split_html(text: str, limit: int = 3900) -> list[str]:
    # Counting serialized UTF-16 is conservative: Telegram limits the parsed text.
    if limit < 64:
        raise ValueError("Message limit too small")

    class Splitter(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.parts = []
            self.current = ""
            self.content = False

        def size(self, text):
            return len(text.encode("utf-16-le")) // 2

        def closing(self):
            return "".join(f"</{tag}>" for tag, _ in reversed(self.stack))

        def flush(self):
            if self.content:
                self.parts.append(self.current + self.closing())
            self.current = "".join(start for _, start in self.stack)
            self.content = False

        def handle_starttag(self, tag, attrs):
            start = self.get_starttag_text()
            if self.size(self.current + start + f"</{tag}>" + self.closing()) > limit:
                self.flush()
            self.current += start
            self.stack.append((tag, start))

        def handle_endtag(self, tag):
            if not self.stack or self.stack[-1][0] != tag:
                raise ValueError("Unbalanced generated Telegram HTML")
            self.current += f"</{tag}>"
            self.stack.pop()

        def handle_data(self, data):
            for character in data:
                encoded = escape(character, quote=False)
                if self.size(self.current + encoded + self.closing()) > limit:
                    self.flush()
                self.current += encoded
                self.content = True

    parser = Splitter()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise ValueError("Unclosed generated Telegram HTML")
    parser.flush()
    return parser.parts or [""]
