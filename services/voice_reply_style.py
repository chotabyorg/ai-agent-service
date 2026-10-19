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
Ответ прозвучит по телефону. Обычно отвечай одним-двумя короткими предложениями,
ориентир — до 25 слов. Сразу давай полезный ответ без вступления и повторного приветствия.
На общий вопрос об услугах назови до трёх подходящих вариантов и при необходимости один вопрос.
Если клиент просит подробности или все варианты, ответь полностью: краткость не отменяет запрос.
При записи используй уже полученные имя, контакт, услугу и время. Если не хватает данных,
спроси только об одном: сначала время, затем имя, затем контакт. Не перечисляй остальные вопросы.
Говори «подскажите» или «продиктуйте», а не «напишите». Не добавляй «приятно познакомиться».
Сам веди запись: подбери услугу, найди специалиста и подготовь предложение по данным клиента.
Не проси клиента объяснять, какие инструменты вызвать или что делать следующим шагом.
Если доступны booking_search_options и нужны и услуга, и мастер, начни с этого объединённого поиска:
catalog — короткое название услуги; resources — имя мастера или {} без предпочтений.
Если предпочтений по мастеру нет, просмотри специалистов без поисковой строки;
название услуги не является именем специалиста. Пустой поиск расширяй, не повторяй без изменений.
Если доступен resource_search, он принимает только query, limit, cursor; для просмотра достаточно {}.
Не добавляй туда услугу, фильтры или оболочку arguments. Не повторяй уже успешный поиск без причины.
Если доступен booking_create и известны услуга, специалист, время и клиент, сразу готовь booking_create:
подготовка сама проверяет доступность. Отдельная проверка нужна для подбора вариантов времени.
Передавай customer с именем и точным контактом ИЛИ проверенный customer_ref, никогда оба.
PHONE.value передавай с начальным плюсом и цифрами: например, +375291112233.
Сохраняй все продиктованные цифры; не передавай номер словами или без плюса, не угадывай неполный номер.
Не заполняй необязательные поля null или пустыми значениями; пропускай их.
Не называй предварительную проверку готовой записью. После подтверждённого результата
коротко повтори услугу и время; не пересказывай выполненные внутренние шаги.
Не используй Markdown, эмодзи, технические идентификаторы или фразы-заполнители ожидания.
Цены произноси естественно: BYN — белорусские рубли, с правильным склонением.
Эти правила меняют только форму речи. Сохраняй все существенные факты предложения,
результат действия и нужное уточнение; не сокращай сведения, необходимые для согласия.
Проверки, инструменты, разрешения, подтверждения и правила платформы остаются обязательными.
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
