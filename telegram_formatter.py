import html
import logging
import re
import unicodedata
from typing import List, Optional, Tuple
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InputRichMessage, InputRichBlockTable, InputRichBlockParagraph, 
    InputRichBlockPreformatted, InputRichBlockSectionHeading,
    InputRichBlockUnion, RichBlockTableCell
)

logger = logging.getLogger(__name__)

def char_width(c: str) -> int:
    """Return display width of a single character taking CJK / wide chars into account."""
    w = unicodedata.east_asian_width(c)
    return 2 if w in ('F', 'W') else 1

def str_display_width(s: str) -> int:
    """Calculate visible width of a string in monospace terminals/fonts."""
    return sum(char_width(c) for c in s)

def pad_string(s: str, target_width: int, align: str = "left") -> str:
    """Pads a string according to its display width."""
    current_width = str_display_width(s)
    padding = max(0, target_width - current_width)
    if align == "right":
        return " " * padding + s
    elif align == "center":
        left = padding // 2
        right = padding - left
        return " " * left + s + " " * right
    else:
        return s + " " * padding

def parse_markdown_table(lines: List[str]) -> Optional[Tuple[List[str], List[str], List[List[str]]]]:
    """
    Parses a block of Markdown table lines into (headers, alignments, rows).
    """
    raw_rows = []
    for line in lines:
        stripped = line.strip().strip("|")
        cells = [c.strip() for c in stripped.split("|")]
        raw_rows.append(cells)

    if len(raw_rows) < 2:
        return None

    headers = raw_rows[0]
    sep_row = raw_rows[1]
    data_rows = raw_rows[2:]

    alignments = []
    for sep in sep_row:
        s = sep.strip()
        if s.startswith(":") and s.endswith(":"):
            alignments.append("center")
        elif s.endswith(":"):
            alignments.append("right")
        else:
            alignments.append("left")

    while len(alignments) < len(headers):
        alignments.append("left")

    normalized_rows = []
    for row in data_rows:
        while len(row) < len(headers):
            row.append("")
        normalized_rows.append(row[:len(headers)])

    return headers, alignments, normalized_rows

def render_box_table(headers: List[str], alignments: List[str], rows: List[List[str]]) -> str:
    """
    Renders table data into a clean, aligned Unicode box table for <pre> blocks.
    """
    num_cols = len(headers)
    col_widths = [str_display_width(h) for h in headers]

    for row in rows:
        for i in range(num_cols):
            w = str_display_width(row[i])
            if w > col_widths[i]:
                col_widths[i] = w

    col_widths = [max(1, w) for w in col_widths]

    top_line = "┌" + "┬".join("─" * (w + 2) for w in col_widths) + "┐"
    header_sep = "├" + "┼".join("─" * (w + 2) for w in col_widths) + "┤"
    bottom_line = "└" + "┴".join("─" * (w + 2) for w in col_widths) + "┘"

    def format_row(cells):
        out = "│"
        for i in range(num_cols):
            val = cells[i] if i < len(cells) else ""
            align = alignments[i] if i < len(alignments) else "left"
            padded = pad_string(val, col_widths[i], align=align)
            out += f" {padded} │"
        return out

    table_lines = [top_line]
    table_lines.append(format_row(headers))
    table_lines.append(header_sep)
    for row in rows:
        table_lines.append(format_row(row))
    table_lines.append(bottom_line)

    return "\n".join(table_lines)

def convert_markdown_tables_in_text(text: str) -> str:
    """
    Scans text for Markdown table blocks and converts them into unicode box tables in ```table.
    """
    lines = text.split("\n")
    output_lines = []
    table_buffer = []
    in_table = False

    def is_table_delimiter(l: str) -> bool:
        stripped = l.strip().strip("|")
        parts = stripped.split("|")
        return len(parts) >= 2 and all(re.match(r'^\s*:?-+:?\s*$', p) for p in parts)

    def is_table_row(l: str) -> bool:
        s = l.strip()
        return s.startswith("|") and s.endswith("|") and s.count("|") >= 2

    i = 0
    while i < len(lines):
        line = lines[i]
        if not in_table and is_table_row(line) and i + 1 < len(lines) and is_table_delimiter(lines[i + 1]):
            in_table = True
            table_buffer = [line, lines[i + 1]]
            i += 2
            continue

        if in_table:
            if is_table_row(line):
                table_buffer.append(line)
                i += 1
                continue
            else:
                in_table = False
                parsed = parse_markdown_table(table_buffer)
                if parsed:
                    h, a, r = parsed
                    box_table = render_box_table(h, a, r)
                    output_lines.append(f"```table\n{box_table}\n```")
                else:
                    output_lines.extend(table_buffer)
                table_buffer = []
                continue

        output_lines.append(line)
        i += 1

    if in_table and table_buffer:
        parsed = parse_markdown_table(table_buffer)
        if parsed:
            h, a, r = parsed
            box_table = render_box_table(h, a, r)
            output_lines.append(f"```table\n{box_table}\n```")
        else:
            output_lines.extend(table_buffer)

    return "\n".join(output_lines)

def build_rich_message_with_native_tables(text: str) -> Optional[InputRichMessage]:
    """
    Parses text containing Markdown tables into a native Telegram InputRichMessage.
    Uses plain strings to avoid the 'text' prefix bug in Telegram clients.
    """
    lines = text.split("\n")
    has_table = False
    for i, line in enumerate(lines[:-1]):
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            next_s = lines[i+1].strip().strip("|")
            parts = next_s.split("|")
            if len(parts) >= 2 and all(re.match(r'^\s*:?-+:?\s*$', p) for p in parts):
                has_table = True
                break

    if not has_table:
        return None

    blocks: List[InputRichBlockUnion] = []
    current_para = []
    table_buffer = []
    in_table = False

    def is_table_delimiter(l: str) -> bool:
        stripped = l.strip().strip("|")
        parts = stripped.split("|")
        return len(parts) >= 2 and all(re.match(r'^\s*:?-+:?\s*$', p) for p in parts)

    def is_table_row(l: str) -> bool:
        s = l.strip()
        return s.startswith("|") and s.endswith("|") and s.count("|") >= 2

    def flush_paragraph():
        if current_para:
            p_text = "\n".join(current_para).strip()
            if p_text:
                if p_text.startswith("#"):
                    clean_heading = p_text.lstrip("#").strip()
                    blocks.append(InputRichBlockSectionHeading(text=clean_heading, size=1))
                else:
                    blocks.append(InputRichBlockParagraph(text=p_text))
            current_para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if not in_table and is_table_row(line) and i + 1 < len(lines) and is_table_delimiter(lines[i + 1]):
            flush_paragraph()
            in_table = True
            table_buffer = [line, lines[i + 1]]
            i += 2
            continue

        if in_table:
            if is_table_row(line):
                table_buffer.append(line)
                i += 1
                continue
            else:
                in_table = False
                parsed = parse_markdown_table(table_buffer)
                if parsed:
                    h, a, r = parsed
                    cells = []
                    header_cells = [RichBlockTableCell(text=c, align=a[col_idx], valign='middle', is_header=True) for col_idx, c in enumerate(h)]
                    cells.append(header_cells)
                    for row in r:
                        row_cells = [RichBlockTableCell(text=c, align=a[col_idx], valign='middle') for col_idx, c in enumerate(row)]
                        cells.append(row_cells)
                    blocks.append(InputRichBlockTable(cells=cells, is_bordered=True, is_striped=True))
                table_buffer = []
                continue

        current_para.append(line)
        i += 1

    if in_table and table_buffer:
        parsed = parse_markdown_table(table_buffer)
        if parsed:
            h, a, r = parsed
            cells = []
            header_cells = [RichBlockTableCell(text=c, align=a[col_idx], valign='middle', is_header=True) for col_idx, c in enumerate(h)]
            cells.append(header_cells)
            for row in r:
                row_cells = [RichBlockTableCell(text=c, align=a[col_idx], valign='middle') for col_idx, c in enumerate(row)]
                cells.append(row_cells)
            blocks.append(InputRichBlockTable(cells=cells, is_bordered=True, is_striped=True))

    flush_paragraph()

    if blocks:
        return InputRichMessage(blocks=blocks)
    return None

def markdown_to_telegram_html(text: str) -> str:
    """
    Transforms Markdown text into Telegram-compliant HTML according to:
    https://core.telegram.org/bots/api#formatting-options
    Supported tags: <b>, <i>, <u>, <s>, <span> (tg-spoiler), <a>, <code>, <pre>, <blockquote>, <blockquote expandable>
    """
    # 0. Convert any Markdown tables to preformatted box tables first
    text = convert_markdown_tables_in_text(text)

    # 1. Protect code blocks ```lang\ncode\n```
    code_blocks = []
    def save_code_block(m):
        lang = m.group(1).strip()
        code = m.group(2)
        escaped_code = html.escape(code.rstrip())
        idx = len(code_blocks)
        if lang:
            tag = f'<pre><code class="language-{html.escape(lang)}">{escaped_code}</code></pre>'
        else:
            tag = f'<pre><code>{escaped_code}</code></pre>'
        code_blocks.append(tag)
        return f"\x00CB{idx}\x00"

    text = re.sub(r'```([a-zA-Z0-9_\-\+\.]*)\r?\n(.*?)```', save_code_block, text, flags=re.DOTALL)

    # 2. Protect inline code `code`
    inline_codes = []
    def save_inline_code(m):
        code = m.group(1)
        escaped_code = html.escape(code)
        idx = len(inline_codes)
        inline_codes.append(f'<code>{escaped_code}</code>')
        return f"\x00IC{idx}\x00"

    text = re.sub(r'`([^`\r\n]+)`', save_inline_code, text)

    # 3. Escape HTML entities for the remaining content
    text = html.escape(text)

    # 4. Links: [text](https://...) -> <a href="...">text</a>
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\s\)]+)\)', r'<a href="\2">\1</a>', text)

    # 5. Spoilers: ||text|| -> <tg-spoiler>text</tg-spoiler>
    text = re.sub(r'\|\|(.*?)\|\|', r'<tg-spoiler>\1</tg-spoiler>', text)

    # 6. Headers
    text = re.sub(r'^(?:&gt;\s*)?###\s+(.+)$', r'<b><i>\1</i></b>', text, flags=re.MULTILINE)
    text = re.sub(r'^(?:&gt;\s*)?##\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^(?:&gt;\s*)?#\s+(.+)$', r'<b><u>\1</u></b>', text, flags=re.MULTILINE)

    # 7. Convert blockquotes (> ... or > [!NOTE] ...)
    lines = text.split("\n")
    processed_lines = []
    in_quote = False
    quote_lines = []
    is_expandable = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("&gt;"):
            content = stripped[4:].strip()
            if not in_quote:
                in_quote = True
                quote_lines = []
                is_expandable = False
                if content.startswith("[!") or len(content) > 120:
                    is_expandable = True
            if content.startswith("[!") and content.endswith("]"):
                alert_type = content[2:-1].upper()
                quote_lines.append(f"<b>📌 {alert_type}:</b>")
                is_expandable = True
            else:
                quote_lines.append(content)
        else:
            if in_quote:
                tag = "<blockquote expandable>" if is_expandable else "<blockquote>"
                processed_lines.append(f"{tag}" + "\n".join(quote_lines) + "</blockquote>")
                in_quote = False
                quote_lines = []
            processed_lines.append(line)

    if in_quote:
        tag = "<blockquote expandable>" if is_expandable else "<blockquote>"
        processed_lines.append(f"{tag}" + "\n".join(quote_lines) + "</blockquote>")

    text = "\n".join(processed_lines)

    # 8. Bold, Underline, Italic, Strikethrough
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<u>\1</u>', text)
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    text = re.sub(r'(?<![a-zA-Z0-9А-Яа-я])\*(.+?)\*(?<![a-zA-Z0-9А-Яа-я])', r'<i>\1</i>', text)
    text = re.sub(r'(?<![a-zA-Z0-9А-Яа-я])_(.+?)_(?<![a-zA-Z0-9А-Яа-я])', r'<i>\1</i>', text)

    # 9. Convert bullet lists (- item -> • item)
    text = re.sub(r'^\s*[-*]\s+', '• ', text, flags=re.MULTILINE)

    # 10. Restore code blocks and inline code
    for i, ic in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", ic)

    for i, cb in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", cb)

    return text

def split_telegram_html(html_text: str, max_length: int = 4000) -> List[str]:
    """
    Splits Telegram HTML into chunks <= max_length while properly closing
    and reopening any open tags (<pre><code>, <blockquote>, etc.)
    """
    if len(html_text) <= max_length:
        return [html_text]

    chunks = []
    lines = html_text.split("\n")
    current_chunk = []
    current_len = 0
    in_pre = False
    pre_tag = ""
    in_quote = False
    quote_tag = ""

    for line in lines:
        line_len = len(line) + 1

        # Track pre block
        if "<pre" in line and not in_pre:
            in_pre = True
            m = re.search(r'(<pre><code[^>]*>)', line)
            pre_tag = m.group(1) if m else "<pre><code>"
        if "</pre>" in line and in_pre:
            in_pre = False
            pre_tag = ""

        # Track quote block
        if "<blockquote" in line and not in_quote:
            in_quote = True
            m = re.search(r'(<blockquote[^>]*>)', line)
            quote_tag = m.group(1) if m else "<blockquote>"
        if "</blockquote>" in line and in_quote:
            in_quote = False
            quote_tag = ""

        if current_len + line_len > max_length:
            if current_chunk:
                chunk_str = "\n".join(current_chunk)
                if in_pre:
                    chunk_str += "</code></pre>"
                if in_quote:
                    chunk_str += "</blockquote>"
                chunks.append(chunk_str)

                current_chunk = []
                current_len = 0
                if in_quote:
                    current_chunk.append(quote_tag)
                    current_len += len(quote_tag) + 1
                if in_pre:
                    current_chunk.append(pre_tag)
                    current_len += len(pre_tag) + 1
            else:
                while len(line) > max_length:
                    chunks.append(line[:max_length])
                    line = line[max_length:]
                if line:
                    current_chunk.append(line)
                    current_len = len(line) + 1
                continue

        current_chunk.append(line)
        current_len += line_len

    if current_chunk:
        chunk_str = "\n".join(current_chunk)
        chunks.append(chunk_str)

    return chunks

async def send_smart_message(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_to_message_id: Optional[int] = None,
    reply_markup=None,
    message_thread_id: Optional[int] = None
):
    """
    Sends message using native Telegram Rich Message (for graphical tables)
    without the 'text' prefix bug, or standard HTML article formatting.
    Supports Telegram forum topics (message_thread_id).
    """
    valid_thread_id = message_thread_id if (message_thread_id and message_thread_id > 0) else None

    # 1. If text contains a table, try native graphical Rich Message first
    rich_msg = build_rich_message_with_native_tables(text)
    if rich_msg:
        try:
            return await bot.send_rich_message(
                chat_id=chat_id,
                message_thread_id=valid_thread_id,
                rich_message=rich_msg,
                reply_parameters={"message_id": reply_to_message_id} if reply_to_message_id else None,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.info(f"Native send_rich_message fallback: {e}")

    # 2. Standard HTML article formatting (with unicode box table in <pre>)
    formatted_html = markdown_to_telegram_html(text)
    chunks = split_telegram_html(formatted_html, max_length=4000)
    last_msg = None

    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            last_msg = await bot.send_message(
                chat_id=chat_id,
                message_thread_id=valid_thread_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to_message_id if i == 0 else None,
                reply_markup=markup
            )
        except TelegramBadRequest as e:
            err_text = str(e).lower()
            if "message thread not found" in err_text and valid_thread_id:
                logger.warning(f"Thread {valid_thread_id} not found or deleted, delivering to main chat {chat_id}")
                valid_thread_id = None
                try:
                    last_msg = await bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=None,
                        text=chunk,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup
                    )
                    continue
                except Exception:
                    pass

            logger.warning(f"HTML parse failed for chunk ({e}), falling back to plain text")
            try:
                plain_chunk = re.sub(r'<[^>]+>', '', chunk)
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=valid_thread_id,
                    text=plain_chunk,
                    parse_mode=None,
                    reply_to_message_id=reply_to_message_id if i == 0 else None,
                    reply_markup=markup
                )
            except Exception as e2:
                logger.error(f"Failed to send plain text message chunk: {e2}")

    return last_msg

