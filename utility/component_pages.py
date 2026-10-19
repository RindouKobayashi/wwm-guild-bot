"""Bound Discord display text in UTF-16 units without dropping content."""


def text_pages(text, budget=2800):
    if budget < 2:
        raise ValueError('Text budget must accommodate Unicode characters')
    pages = []
    while text:
        units = 0
        end = 0
        for char in text:
            size = 2 if ord(char) > 0xFFFF else 1
            if units + size > budget:
                break
            units += size
            end += 1
        cut = text.rfind('\n', 0, end) + 1 if end < len(text) else end
        if cut < end // 2:
            cut = end
        pages.append(text[:cut])
        text = text[cut:]
    return pages or ['*No items on this page*']


def item_pages(sections, budget=2800):
    """Pack whole items; oversized items retain full text as an attachment."""
    pages = []
    units = lambda value: len(value.encode('utf-16-le')) // 2
    for label, blocks in sections:
        current = ''
        for block in blocks:
            text = '\n'.join(block)
            if not text.strip():
                continue
            if units(text) > budget:
                if current:
                    pages.append((label, current, None))
                    current = ''
                pages.append((label, 'This item exceeds Discord’s text limit. Its complete details are attached as a text file.', text))
            elif current and units(current + '\n\n' + text) > budget:
                pages.append((label, current, None))
                current = text
            else:
                current = current + '\n\n' + text if current else text
        if current:
            pages.append((label, current, None))
    return pages
