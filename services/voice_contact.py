"""Conservative phone facts from customer speech, never a CRM identity or grant."""
import json
import re

_UNITS = dict(zip(('ноль', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять'), range(10)))
_UNITS.update({'нуль': 0, 'одна': 1, 'две': 2})
_TEENS = dict(zip(('десять', 'одиннадцать', 'двенадцать', 'тринадцать', 'четырнадцать',
    'пятнадцать', 'шестнадцать', 'семнадцать', 'восемнадцать', 'девятнадцать'), range(10, 20)))
_TENS = dict(zip(('двадцать', 'тридцать', 'сорок', 'пятьдесят', 'шестьдесят', 'семьдесят', 'восемьдесят', 'девяносто'), range(20, 100, 10)))
_HUNDREDS = dict(zip(('сто', 'двести', 'триста', 'четыреста', 'пятьсот', 'шестьсот', 'семьсот', 'восемьсот', 'девятьсот'), range(100, 1000, 100)))
_NUMBER_WORDS = frozenset(_UNITS) | frozenset(_TEENS) | frozenset(_TENS) | frozenset(_HUNDREDS)
_CONTACT = re.compile(r'\bтелефон(?:а|ом)?\b|\b(?:контактный|мой)\s+номер\b|\bномер\s+для\s+связи\b')
_ANCHOR = re.compile(_CONTACT.pattern + r'|\bплюс\b|\+')
_TOKEN = re.compile(r'[а-яё]+|[0-9]+|\+')
_PHONE = re.compile(r'\+[1-9][0-9]{7,14}\Z')


def _number_run(tokens):
    """Return a phone and consumed prefix length, preserving spoken zeros."""
    parts = []
    index = 0
    if tokens and tokens[0] in {'плюс', '+'}:
        index += 1
    while index < len(tokens) and index < 48:
        word = tokens[index]
        if word.isascii() and word.isdecimal():
            parts.append(word)
            index += 1
            continue
        value = 0
        consumed = False
        if word in _HUNDREDS:
            value = _HUNDREDS[word]
            index += 1
            consumed = True
        if index < len(tokens) and tokens[index] in _TEENS:
            value += _TEENS[tokens[index]]
            index += 1
            consumed = True
        else:
            if index < len(tokens) and tokens[index] in _TENS:
                value += _TENS[tokens[index]]
                index += 1
                consumed = True
            if index < len(tokens) and tokens[index] in _UNITS:
                # Zero is its own group: «сорок ноль один» is 40 0 1.
                if tokens[index] not in {'ноль', 'нуль'} or not consumed:
                    value += _UNITS[tokens[index]]
                    index += 1
                    consumed = True
        if not consumed:
            break
        parts.append(str(value))
    if index < len(tokens) and tokens[index] in {'тысяч', 'тысяча', 'тысячи', 'миллион', 'миллионов'}:
        return None, index
    digits = ''.join(parts)
    return ('+' + digits if _PHONE.fullmatch('+' + digits) else None), index


def voice_phone_facts(request, caller_phone=''):
    """Use only the latest explicit customer phone span in this frozen window.

    An assistant quote, system text, price or appointment date is not a source.
    Missing/ambiguous speech never supplies guessed digits or a default number.
    """
    if request is None or getattr(request, 'channel', None) != 'VOICE':
        return None
    for position in range(len(request.messages) - 1, -1, -1):
        message = request.messages[position]
        if (message.sequence > request.input_last_sequence
                or message.actor != 'CUSTOMER' or message.direction != 'INBOUND'):
            continue
        text = message.text.casefold().replace('ё', 'е')
        anchors = list(_ANCHOR.finditer(text))
        if not anchors:
            # A caller can answer a phone question with digits alone. A date,
            # price, assistant quotation or unsolicited number is insufficient.
            previous_agent = next((item for item in reversed(request.messages[:position])
                if item.actor == 'AGENT' and item.direction == 'OUTBOUND'), None)
            tokens = _TOKEN.findall(text)
            if (previous_agent is not None and _CONTACT.search(previous_agent.text.casefold())
                    and tokens and all(token in _NUMBER_WORDS or token.isdecimal() for token in tokens)):
                number, _ = _number_run(tokens)
                return (number,) if number else ()
            continue
        numbers = set()
        for anchor in anchors[:4]:
            tokens = _TOKEN.findall(text[anchor.end():])[:50]
            number, consumed = _number_run(tokens)
            if number:
                numbers.add(number)
                # A choice between numbers is ambiguous. A later choice of
                # appointment dates must not erase a complete contact.
                alternative = tokens[consumed + 1:]
                if tokens[consumed:consumed + 1] == ['или'] and alternative and (
                        alternative[0] in _NUMBER_WORDS | {'плюс', '+'} or alternative[0].isdecimal()):
                    other, _ = _number_run(alternative)
                    if other is None:
                        return ()
                    numbers.add(other)
        return tuple(numbers) if len(anchors) <= 4 and len(numbers) == 1 else ()
    return (caller_phone,) if isinstance(caller_phone, str) and _PHONE.fullmatch(caller_phone) else ()


def voice_phone_hint(request, caller_phone=''):
    """Render bounded derived data without editing the transcript or authority."""
    numbers = voice_phone_facts(request, caller_phone)
    if numbers is None:
        return ''
    if len(numbers) == 1:
        data = json.dumps({'phone': numbers[0]}, ensure_ascii=False)
        return ('\n\n<customer_phone_facts>Телефон из последней явной клиентской реплики '
            '(или входящий номер, если другой не назван): ' + data
            + '. Это данные для подготовки, не подтверждение и не CRM identity. '
            'Используй ровно эти цифры; обычная проверка контакта и отдельное согласие обязательны.</customer_phone_facts>')
    return ('\n\n<customer_phone_facts>Однозначный полный контактный номер пока неизвестен. '
        'Не подставляй нули, пример номера или выдуманные цифры. Когда нужен контакт для записи, '
        'попроси один полный номер с кодом страны. До уточнения контакта не вызывай подготовку '
        'записи с полем customer.</customer_phone_facts>')
