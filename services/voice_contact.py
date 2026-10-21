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
_CONTACT = re.compile(r'\bтелефон(?:а|ом|е|у)?\b|\b(?:контактный|мой|ваш)\s+номер\b|\bномер\s+для\s+связи\b')
_ANCHOR = re.compile(_CONTACT.pattern + r'|\bплюс\b|\+')
_TOKEN = re.compile(r'[а-яё]+|[0-9]+|\+')
_PHONE = re.compile(r'\+[1-9][0-9]{7,14}\Z')
_REPLACEMENT = re.compile(r'\b(?:нет|вернее|точнее|исправляю|поправка)\b')
_LAST_DIGIT = re.compile(r'\bпоследн(?:яя|юю)\s+цифр[ау]\b'
    r'(?:\s+(?:телефон[ае]?|(?:в\s+)?номер[ае]?))?')
_DIGIT_CORRECTION = re.compile(_LAST_DIGIT.pattern
    + r'|\bпоследни[ех]\s+(?:две|три|[0-9]+)\s+цифр\w*\b')
_OTHER_VALUE = re.compile(r'^\s+(?:в\s+)?(?:код[ае]|дат[ые]|времени|цен[ые]|сумм[ые])\b')


def _number_run(tokens):
    """Return a phone and consumed prefix length, preserving spoken zeros."""
    parts = []
    index = 0
    explicit_plus = bool(tokens) and tokens[0] in {'плюс', '+'}
    if explicit_plus:
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
    if not explicit_plus and digits.startswith('8'):
        # A domestic trunk prefix is not an international country code.
        return None, index
    return ('+' + digits if _PHONE.fullmatch('+' + digits) else None), index


def _answer_phone_facts(tokens):
    number, consumed = _number_run(tokens)
    if tokens[consumed:consumed + 1] == ['или']:
        return ()
    return (number,) if number else ()


def _explicit_phone_facts(text):
    """None means no contact span; an empty tuple means unusable contact."""
    if len(list(_ANCHOR.finditer(text))) > 4:
        return ()
    for replacement in reversed(list(_REPLACEMENT.finditer(text))):
        if not _ANCHOR.search(text[:replacement.start()]):
            continue
        tail = text[replacement.end():]
        tokens = _TOKEN.findall(tail)
        if _ANCHOR.search(tail):
            text = tail
            break
        if tokens and (tokens[0] in _NUMBER_WORDS or tokens[0].isdecimal()):
            return _answer_phone_facts(tokens)
    anchors = list(_ANCHOR.finditer(text))
    if not anchors:
        return None
    numbers = set()
    for anchor in anchors[:4]:
        tokens = _TOKEN.findall(text[anchor.end():])[:50]
        if anchor.group() in {'плюс', '+'}:
            tokens.insert(0, anchor.group())
        number, consumed = _number_run(tokens)
        if number:
            numbers.add(number)
            # Date alternatives after a complete contact are independent.
            alternative = tokens[consumed + 1:]
            if tokens[consumed:consumed + 1] == ['или'] and alternative and (
                    alternative[0] in _NUMBER_WORDS | {'плюс', '+'} or alternative[0].isdecimal()):
                other, _ = _number_run(alternative)
                if other is None:
                    return ()
                numbers.add(other)
    return tuple(numbers) if len(anchors) <= 4 and len(numbers) == 1 else ()


def _corrected_digit(text):
    tokens = _TOKEN.findall(text)
    if tokens[:1] in (['это'], ['на']):
        tokens = tokens[1:]
    if len(tokens) == 4 and tokens[0] == 'не' and tokens[2] == 'а':
        tokens = [tokens[3]] if _digit(tokens[1]) is not None else []
    elif len(tokens) == 4 and tokens[1:3] == ['а', 'не']:
        tokens = [tokens[0]] if _digit(tokens[3]) is not None else []
    return _digit(tokens[0]) if len(tokens) == 1 else None


def _digit(token):
    if token in _UNITS:
        return str(_UNITS[token])
    return token if len(token) == 1 and token.isascii() and token.isdecimal() else None


def _window_phone_facts(messages, input_last_sequence, caller_phone, depth=0):
    # A chain of corrections cannot cause unbounded recursive context work.
    if depth >= 8:
        return ()
    for position in range(len(messages) - 1, -1, -1):
        message = messages[position]
        if (message.sequence > input_last_sequence
                or message.actor != 'CUSTOMER' or message.direction != 'INBOUND'):
            continue
        text = message.text.casefold().replace('ё', 'е')
        correction = _DIGIT_CORRECTION.search(text)
        if correction and not _OTHER_VALUE.match(text[correction.end():]):
            prefix_text = text[:correction.start()]
            prefix = _explicit_phone_facts(prefix_text)
            if prefix == () and not any(token in _NUMBER_WORDS | {'плюс', '+'} or token.isdecimal()
                    for token in _TOKEN.findall(prefix_text)):
                # «В телефоне последняя цифра...» identifies the old contact;
                # «Телефон +375..., последняя цифра...» supplies a new prefix.
                prefix = None
            previous = next((item for item in reversed(messages[:position])
                if item.sequence <= input_last_sequence and (
                    (item.actor == 'CUSTOMER' and item.direction == 'INBOUND')
                    or (item.actor == 'AGENT' and item.direction == 'OUTBOUND'))), None)
            contact_context = prefix is not None or _CONTACT.search(text) or (
                previous is not None and (_CONTACT.search(previous.text.casefold()) or (
                    previous.actor == 'CUSTOMER' and (_ANCHOR.search(previous.text.casefold())
                        or _DIGIT_CORRECTION.search(previous.text.casefold())))))
            if contact_context:
                if len(list(_DIGIT_CORRECTION.finditer(text))) != 1 or not _LAST_DIGIT.match(text, correction.start()):
                    return ()
                digit = _corrected_digit(text[correction.end():])
                old = prefix if prefix is not None else _window_phone_facts(
                    messages[:position], input_last_sequence, caller_phone, depth + 1)
                return (old[0][:-1] + digit,) if digit is not None and len(old) == 1 else ()
            # An unqualified correction cannot silently inherit an old contact.
            return ()
        explicit = _explicit_phone_facts(text)
        if explicit is not None:
            return explicit
        # Bare digits require an actual preceding assistant contact question.
        previous_agent = next((item for item in reversed(messages[:position])
            if item.sequence <= input_last_sequence
            and item.actor == 'AGENT' and item.direction == 'OUTBOUND'), None)
        tokens = _TOKEN.findall(text)
        if (previous_agent is not None and _CONTACT.search(previous_agent.text.casefold())
                and tokens and (tokens[0] in _NUMBER_WORDS or tokens[0].isdecimal())):
            return _answer_phone_facts(tokens)
    return (caller_phone,) if isinstance(caller_phone, str) and _PHONE.fullmatch(caller_phone) else ()


def voice_phone_facts(request, caller_phone=''):
    """Use the latest customer contact and explicit corrections in this window.

    An assistant quote, system text, price or appointment date is not a source.
    Missing/ambiguous speech never supplies guessed digits or a default number.
    """
    if request is None or getattr(request, 'channel', None) != 'VOICE':
        return None
    return _window_phone_facts(request.messages, request.input_last_sequence, caller_phone)


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
        'Не подставляй нули, пример номера или выдуманные цифры. Для новой записи, если нет '
        'проверенного customer_ref или названного e-mail, сначала обычной репликой попроси один '
        'полный номер с кодом страны. До ответа не ищи услугу или специалиста ради этой записи '
        'и не вызывай подготовку с полем customer. После номера продолжи с уже известными '
        'данными; не спрашивай их заново. Если клиент отдельно спрашивает сведения об услуге '
        'или специалисте, нужный READ допустим и без номера.</customer_phone_facts>')
