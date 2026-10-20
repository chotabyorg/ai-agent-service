"""Opt-in spoken presentation, with no execution or confirmation authority."""

import re


CONNECTION_CHECK = 'Вы ещё на линии?'


def is_idle_input(request):
    """Recognize only a standalone current Voice control marker."""
    selected = request.selected_messages
    return (getattr(request.channel, 'value', request.channel) == 'VOICE'
            and len(selected) == 1 and selected[0].actor == 'CUSTOMER'
            and selected[0].text == '[silence]')


def _bare_presence(text):
    words = re.sub(r'[^\w\s]', ' ', text.casefold()).split()
    return bool(words and len(words) <= 8 and all(word in {
        'да', 'ага', 'угу', 'алло', 'я', 'здесь', 'тут', 'на', 'линии', 'месте',
        'ещё', 'еще', 'слышу', 'слушаю', 'вас', 'yes', 'hello', 'here'} for word in words))


def replying_to_connection_check(request):
    """A bare presence reply after our control question is not business consent.

    The question can be absent from canonical history after interrupted playback.
    Never treat an older idle event behind a business question as this situation.
    """
    selected = request.selected_messages
    if (not selected or request.input_last_sequence != request.context_last_sequence
            or selected[-1].actor != 'CUSTOMER' or not _bare_presence(selected[-1].text)):
        return False
    previous = request.messages[:request.selected_local_indices[-1]]
    # Several interruptions can leave consecutive presence replies without a
    # delivered business question. None of them supplies business consent.
    while previous and previous[-1].actor == 'CUSTOMER' and _bare_presence(previous[-1].text):
        previous = previous[:-1]
    if previous and previous[-1].actor == 'AGENT' and previous[-1].text == CONNECTION_CHECK:
        previous = previous[:-1]
    return bool(previous and previous[-1].actor == 'CUSTOMER'
                and previous[-1].text == '[silence]')


def past_idle_communication(request, communication):
    """Label historical control input without changing canonical selection.

    Preserve every position and other message verbatim. The request, request
    identity and delivery facts are never edited or inferred here.
    """
    latest = request.selected_local_indices[-1]
    return [({'role': 'user', 'content': '[Past voice control event: caller was silent then; '
               'this is not the current customer reply.]'}
             if index < latest and message.actor == 'CUSTOMER' and message.text == '[silence]'
             else item)
            for index, (message, item) in enumerate(zip(request.messages, communication, strict=True))]

CONCISE_SPEECH = """<voice_reply_style>
Ответ прозвучит по телефону: обычно одно-два коротких предложения, до 25 слов,
без вступления, повторного приветствия, Markdown, эмодзи и фраз ожидания.
На общий вопрос назови до трёх вариантов; по просьбе о подробностях ответь полностью.
Говори «подскажите» или «продиктуйте». Не добавляй «приятно познакомиться».
Правила сбора данных, подготовки предложения и отдельного согласия заданы platform_policy:
не повторяй уже известные имя, контакт, услугу и время, не сокращай существенные факты.
Сам веди запись; не спрашивай клиента, какие инструменты вызвать.
Если нужны услуга и мастер, используй booking_search_options: catalog — название услуги,
resources — имя мастера или {} без предпочтений; услуга не является именем мастера.
Для resource_search допустимы только query, limit, cursor. Пустой поиск расширяй;
успешный поиск без причины не повторяй. При известном времени сразу готовь booking_create,
не добавляй отдельную проверку доступности, если не подбираешь варианты времени.
В booking_create выбери ровно один вариант клиента: customer_ref ИЛИ customer.
После SINGLE используй возвращённый customer_ref, а ключ customer пропусти целиком.
После NONE передай customer с известными именем и контактом; при ошибке/конфликте уточни данные.
PHONE.value — плюс и все продиктованные цифры, например +375291112233; не угадывай неполный номер.
Необязательные поля пропускай вместо null и пустых значений.
После авторитетного успешного результата коротко повтори услугу и время без внутренних шагов.
Цены произноси естественно: BYN — белорусские рубли с правильным склонением.
Это только форма речи; проверки, полномочия и согласие остаются обязательными.
</voice_reply_style>"""


def dialogue_state(request):
    """Derive a presentation hint from current input, never from old silence.

    A question can be heard in a partial that the caller interrupts before a
    final assistant Message exists. Do not infer a new connection check from
    the old silence marker in that canonical window.
    """
    selected = request.selected_messages
    latest = next((message for message in reversed(selected)
                   if message.actor == 'CUSTOMER'), None)
    if latest is not None and latest.text == '[silence]':
        return ('Текущий вход — сигнал тишины. Один раз проверь связь. '
                'Не отменяй и не подтверждай запись из-за тишины.')
    return ('Клиент сейчас ответил; это не сигнал тишины. Не спрашивай «Вы на линии?» '
            'и не повторяй проверку связи из истории. Если ответ — «да» после проверки связи, '
            'продолжи незавершённый запрос клиента с уже названными данными. '
            'После технического сбоя восстанови ход записи по истории и проверь факты инструментами. '
            'Ответ о присутствии на линии сам по себе не означает согласия на бизнес-действие: '
            'сохраняй обязательное подтверждение подготовленного предложения.')
