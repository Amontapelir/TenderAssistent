"""Определение формата файла и извлечение текста.

Поддержан Word (.docx): абзацы и таблицы в порядке следования. Остальные форматы
распознаются и называются, но текст из них не извлекается: вызывающий код обязан
показать это пользователю.
"""
import io
import zipfile
import xml.etree.ElementTree as ET

from app import limits

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
READABLE = ("docx", "text")


class Unreadable(RuntimeError):
    """Файл распознан, но прочитать его нельзя. Текст исключения показывается пользователю."""


def detect(data: bytes) -> str:
    """Формат по содержимому, а не по имени: docx, xlsx, zip, pdf, doc, rtf, html, text, unknown."""
    head = data[:8]
    if head.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile:
            return "unknown"
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "zip"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "doc"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(b"Rar!") or head.startswith(b"7z\xbc\xaf"):
        return "zip"
    sample = data[:2000].lstrip().lower()
    if sample.startswith(b"<!doctype html") or sample.startswith(b"<html"):
        return "html"
    try:
        data[:4000].decode("utf-8")
        return "text" if data.strip() else "unknown"
    except UnicodeDecodeError:
        return "unknown"


_NAMES = {
    "pdf": "PDF", "doc": "старый формат Word (.doc)", "rtf": "RTF", "xlsx": "таблица Excel",
    "zip": "архив", "html": "веб-страница вместо файла", "unknown": "неизвестный формат",
}


def extract(data: bytes, kind: str) -> str:
    if kind == "docx":
        return _docx(data)
    if kind == "text":
        return data.decode("utf-8", "replace")
    raise Unreadable(f"Формат не поддержан: {_NAMES.get(kind, kind)}. Текст не извлечён.")


def _docx(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        info = archive.getinfo("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise Unreadable("Файл Word повреждён: в нём нет текста документа.") from None
    if info.file_size > limits.DOC_XML_MAX_BYTES:
        raise Unreadable(
            f"Текст документа после распаковки занимает {info.file_size // 2**20} МБ, "
            f"предел {limits.DOC_XML_MAX_BYTES // 2**20} МБ. Файл не прочитан."
        )
    xml = archive.read(info)
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise Unreadable("В файле Word есть объявления, которых в документе быть не должно. Файл не прочитан.")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise Unreadable(f"Файл Word повреждён: {e}") from None
    body = root.find(f"{W}body")
    lines: list[str] = []
    if body is not None:
        _block(body, lines)
    return "\n".join(lines)


def _paragraph(p: ET.Element) -> str:
    out: list[str] = []
    for node in p.iter():
        if node.tag == f"{W}t":
            out.append(node.text or "")
        elif node.tag == f"{W}tab":
            out.append("\t")
        elif node.tag in (f"{W}br", f"{W}cr"):
            out.append(" ")
    return "".join(out).strip()


def _cell(cell: ET.Element) -> str:
    inner: list[str] = []
    _block(cell, inner)
    return " ".join(inner)


def _block(parent: ET.Element, lines: list[str]) -> None:
    """Абзацы и таблицы в порядке следования. Строка таблицы: ячейки через « | »."""
    for child in parent:
        if child.tag == f"{W}p":
            text = _paragraph(child)
            if text:
                lines.append(text)
        elif child.tag == f"{W}tbl":
            for row in child.iter(f"{W}tr"):
                # Только ячейки этой строки, не вложенных таблиц.
                cells = [_cell(c) for c in row.findall(f"{W}tc")]
                if any(cells):
                    lines.append(" | ".join(cells))
        elif child.tag in (f"{W}sdt", f"{W}sdtContent", f"{W}customXml", f"{W}smartTag"):
            _block(child, lines)
