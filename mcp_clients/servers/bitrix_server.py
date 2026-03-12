import os
import sys
import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from fast_bitrix24 import Bitrix

# Инициализируем сервер MCP
mcp = FastMCP("bitrix24")

# Инициализируем клиент Bitrix24
B24_WEBHOOK_URL = os.environ.get("B24_WEBHOOK_URL")
if not B24_WEBHOOK_URL:
    raise ValueError("Необходимо установить переменную окружения B24_WEBHOOK_URL.")

b24 = Bitrix(B24_WEBHOOK_URL)


async def safe_call(method: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Безопасный асинхронный вызов API Bitrix24 с защитой от пустых параметров."""
    if params is None:
        params = {}
    clean_params = {k: v for k, v in params.items() if v is not None}
    try:
        if "list" in method:
            # get_all автоматически обрабатывает пагинацию
            return await asyncio.to_thread(b24.get_all, method, clean_params)
        else:
            return await asyncio.to_thread(b24.call, method, clean_params)
    except Exception as e:
        print(f"Ошибка при вызове API '{method}': {e}", file=sys.stderr)
        return {"error": str(e)}


# ==========================================
# ИНСТРУМЕНТЫ ДЛЯ РАБОТЫ С КОНТАКТАМИ
# ==========================================

@mcp.tool()
async def get_contact(contact_id: int) -> dict:
    """Получение полной информации о контакте по ID."""
    result = await safe_call("crm.contact.get", {"ID": contact_id})
    if isinstance(result, dict) and "error" in result:
        return {"error": f"Контакт с ID={contact_id} не найден. Детали: {result['error']}"}
    return result

@mcp.tool()
async def search_contacts(query: str, search_type: str = "name", limit: int = 100) -> dict:
    """Поиск контактов по имени, телефону или email. search_type: 'name', 'phone', 'email'"""
    if search_type not in ["name", "phone", "email"]:
        return {"error": "Недопустимый тип поиска. Используйте: name, phone или email"}

    filter_params = {}
    if search_type == "name":
        filter_params["%NAME"] = query
    elif search_type == "phone":
        filter_params["%PHONE"] = query
    elif search_type == "email":
        filter_params["%EMAIL"] = query

    contacts = await safe_call("crm.contact.list", {"filter": filter_params, "select": ["*"]})
    if isinstance(contacts, dict) and "error" in contacts: return contacts
    if isinstance(contacts, list) and limit > 0: contacts = contacts[:limit]

    return {"query": query, "search_type": search_type, "total": len(contacts) if isinstance(contacts, list) else 0, "contacts": contacts}

@mcp.tool()
async def find_contact(name: str = "", phone: str = "") -> list:
    """Обычный поиск контакта по имени или телефону."""
    filter_params = {}
    if name: filter_params["%NAME"] = name
    if phone: filter_params["%PHONE"] = phone
    result = await safe_call("crm.contact.list", {"filter": filter_params, "select": ["ID", "NAME", "LAST_NAME", "PHONE"]})
    return result if isinstance(result, list) else[]

@mcp.tool()
async def list_contacts(limit: int = 200, company_id: int = None) -> dict:
    """Получение списка контактов с возможностью фильтрации по компании."""
    filter_params = {}
    if company_id: filter_params["COMPANY_ID"] = company_id
    contacts = await safe_call("crm.contact.list", {"filter": filter_params, "select": ["*"]})
    if isinstance(contacts, list) and limit > 0: contacts = contacts[:limit]
    return {"total": len(contacts) if isinstance(contacts, list) else 0, "filters": {"company_id": company_id}, "contacts": contacts}

@mcp.tool()
async def add_contact(name: str, last_name: str = "", phone: str = "", email: str = "") -> dict:
    """Добавить новый контакт в CRM."""
    fields = {"NAME": name, "LAST_NAME": last_name}
    if phone: fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
    if email: fields["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "HOME"}]

    result = await safe_call("crm.contact.add", {"fields": fields})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "contact_id": result}

@mcp.tool()
async def update_contact_communication(contact_id: int, phones: list[dict] = None, emails: list[dict] = None) -> dict:
    """Обновить или удалить телефоны и email контакта."""
    fields = {}
    if phones is not None: fields["PHONE"] = phones
    if emails is not None: fields["EMAIL"] = emails
    result = await safe_call("crm.contact.update", {"ID": contact_id, "fields": fields})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": bool(result), "result": result}


# ==========================================
# КОМПАНИИ И ЛИДЫ
# ==========================================

@mcp.tool()
async def get_lead(lead_id: int) -> dict:
    """Получение детальной информации о лиде по ID."""
    result = await safe_call("crm.lead.get", {"ID": lead_id})
    if isinstance(result, dict) and "error" in result: return {"error": f"Лид с ID={lead_id} не найден."}
    return result

@mcp.tool()
async def add_lead(name: str, last_name: str = "", company_title: str = "", phone: str = "", email: str = "", comments: str = "") -> dict:
    """Создать новый лид (заявку)."""
    title = f"Заявка: {name} {last_name}".strip()
    if company_title: title += f" — {company_title}"
    fields = {"TITLE": title, "NAME": name, "LAST_NAME": last_name, "COMPANY_TITLE": company_title, "STATUS_ID": "NEW"}
    if comments: fields["COMMENTS"] = comments
    if phone: fields["PHONE"] =[{"VALUE": phone, "VALUE_TYPE": "WORK"}]
    if email: fields["EMAIL"] =[{"VALUE": email, "VALUE_TYPE": "WORK"}]

    result = await safe_call("crm.lead.add", {"fields": fields})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "lead_id": result}

@mcp.tool()
async def update_lead_status(lead_id: int, status_id: str) -> dict:
    """Обновление статуса лида."""
    result = await safe_call("crm.lead.update", {"ID": lead_id, "fields": {"STATUS_ID": status_id}})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "message": f"Статус лида {lead_id} изменен на {status_id}"}

@mcp.tool()
async def get_company(company_id: int) -> dict:
    """Получение данных компании по ID."""
    result = await safe_call("crm.company.get", {"ID": company_id})
    if isinstance(result, dict) and "error" in result: return {"error": f"Компания с ID={company_id} не найдена."}
    return result

@mcp.tool()
async def add_company(title: str, phone: str = "", email: str = "") -> dict:
    """Создать новую компанию."""
    fields = {"TITLE": title, "COMPANY_TYPE": "CUSTOMER"}
    if phone: fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
    if email: fields["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "WORK"}]
    result = await safe_call("crm.company.add", {"fields": fields})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "company_id": result}


# ==========================================
# ПОСТАВЩИКИ И РЕКВИЗИТЫ
# ==========================================

@mcp.tool()
async def get_requisite_presets() -> list:
    """Получить шаблоны реквизитов."""
    result = await safe_call("crm.requisite.preset.list", {"select": ["ID", "NAME"]})
    return result if isinstance(result, list) else[]

@mcp.tool()
async def add_company_with_requisites(title: str, preset_id: int, inn: str = "", phone: str = "", address: str = "") -> dict:
    """Создать компанию и сразу заполнить ее реквизиты (ИНН, Адрес)."""
    company_res = await add_company(title=title, phone=phone)
    if "error" in company_res or not company_res.get("success"): return company_res

    company_id = company_res["company_id"]
    req_fields = {"ENTITY_TYPE_ID": 4, "ENTITY_ID": company_id, "PRESET_ID": preset_id, "ACTIVE": "Y", "NAME": title}
    if inn: req_fields["RQ_INN"] = inn
    requisite_id = await safe_call("crm.requisite.add", {"fields": req_fields})

    address_added = False
    if address and requisite_id:
        addr_fields = {"TYPE_ID": 1, "ENTITY_TYPE_ID": 8, "ENTITY_ID": requisite_id, "ADDRESS_1": address}
        res = await safe_call("crm.address.add", {"fields": addr_fields})
        address_added = bool(res)

    return {"success": True, "company_id": company_id, "requisite_id": requisite_id, "address_added": address_added}

@mcp.tool()
async def get_client_addresses(entity_type_id: int, entity_id: int) -> list:
    """Получить адреса клиента из его реквизитов."""
    requisites = await safe_call("crm.requisite.list", {"filter": {"ENTITY_TYPE_ID": entity_type_id, "ENTITY_ID": entity_id}, "select": ["ID"]})
    if not requisites or not isinstance(requisites, list): return []

    addresses =[]
    for req in requisites:
        addr = await safe_call("crm.address.list", {"filter": {"ENTITY_TYPE_ID": 8, "ENTITY_ID": req["ID"]}})
        if isinstance(addr, list): addresses.extend(addr)
    return addresses

@mcp.tool()
async def get_suppliers(entity_type: str = "CONTACT") -> list:
    """Получить список поставщиков (CONTACT или COMPANY)."""
    code = "CATALOG_CONTRACTOR_CONTACT" if entity_type.upper() == "CONTACT" else "CATALOG_CONTRACTOR_COMPANY"
    e_id = 3 if entity_type.upper() == "CONTACT" else 4
    cat_res = await safe_call("crm.category.list", {"entityTypeId": e_id, "filter": {"code": code}})
    cat_list = cat_res.get("categories",[]) if isinstance(cat_res, dict) else cat_res
    if not cat_list or not isinstance(cat_list, list): return []

    category_id = cat_list[0].get("id")
    items = await safe_call("crm.item.list", {"entityTypeId": e_id, "select": ["id", "name", "lastName", "title", "categoryId"], "filter": {"categoryId": category_id}})
    if isinstance(items, dict) and "items" in items: items = items["items"]
    return items if isinstance(items, list) else[]

@mcp.tool()
async def add_supplier(entity_type: str, name_or_title: str, last_name: str = "", phone: str = "", email: str = "", comments: str = "") -> dict:
    """Создать поставщика (Контакт или Компания)."""
    entity_type_id = 3 if entity_type.upper() == "CONTACT" else 4
    category_code = "CATALOG_CONTRACTOR_CONTACT" if entity_type.upper() == "CONTACT" else "CATALOG_CONTRACTOR_COMPANY"

    categories_data = await safe_call("crm.category.list", {"entityTypeId": entity_type_id, "filter": {"code": category_code}})
    cat_list = categories_data.get("categories",[]) if isinstance(categories_data, dict) else categories_data
    if not cat_list or not isinstance(cat_list, list): return {"error": f"Категория поставщиков не найдена."}

    category_id = cat_list[0].get("id")
    fields = {"categoryId": category_id, "comments": comments}
    if entity_type.upper() == "CONTACT":
        fields["name"] = name_or_title
        fields["lastName"] = last_name
    else:
        fields["title"] = name_or_title

    fm =[]
    if phone: fm.append({"typeId": "PHONE", "valueType": "WORK", "value": phone})
    if email: fm.append({"typeId": "EMAIL", "valueType": "WORK", "value": email})
    if fm: fields["fm"] = fm

    item_data = await safe_call("crm.item.add", {"entityTypeId": entity_type_id, "fields": fields})
    if isinstance(item_data, dict) and "error" in item_data: return item_data
    return {"success": True, "supplier_id": item_data.get("item", {}).get("id") if isinstance(item_data, dict) else None}


# ==========================================
# СДЕЛКИ И ТОВАРЫ
# ==========================================

@mcp.tool()
async def get_deal(deal_id: int) -> dict:
    """Получить информацию о сделке."""
    result = await safe_call("crm.deal.get", {"ID": deal_id})
    if isinstance(result, dict) and "error" in result: return {"error": f"Сделка с ID={deal_id} не найдена."}
    return result

@mcp.tool()
async def list_deals(active_only: bool = False, contact_id: int = None, company_id: int = None, limit: int = -1) -> dict:
    """Получить список сделок по фильтрам."""
    filter_params = {}
    if active_only: filter_params["CLOSED"] = "N"
    if contact_id: filter_params["CONTACT_ID"] = contact_id
    if company_id: filter_params["COMPANY_ID"] = company_id

    deals = await safe_call("crm.deal.list", {"filter": filter_params, "select": ["*"]})
    if isinstance(deals, list) and limit > 0: deals = deals[:limit]
    return {"total": len(deals) if isinstance(deals, list) else 0, "deals": deals}

@mcp.tool()
async def add_deal_with_company(deal_title: str, company_title: str, preset_id: int, inn: str = "", phone: str = "") -> dict:
    """Создать компанию с реквизитами и сразу привязать к ней новую сделку."""
    company_res = await add_company_with_requisites(title=company_title, preset_id=preset_id, inn=inn, phone=phone)
    if "error" in company_res or not company_res.get("success"): return company_res

    deal_fields = {"TITLE": deal_title, "COMPANY_ID": company_res.get("company_id"), "REQUISITE_ID": company_res.get("requisite_id")}
    deal_id = await safe_call("crm.deal.add", {"fields": deal_fields})
    return {"success": True, "deal_id": deal_id, "company_id": company_res.get("company_id")}

@mcp.tool()
async def update_deal_stage(deal_id: int, stage_id: str) -> dict:
    """Обновить стадию сделки."""
    result = await safe_call("crm.deal.update", {"ID": deal_id, "fields": {"STAGE_ID": stage_id}})
    success = bool(result) and not (isinstance(result, dict) and "error" in result)
    return {"success": success, "message": f"Стадия сделки ID={deal_id} {'обновлена' if success else 'ошибка'} на {stage_id}"}

@mcp.tool()
async def get_deal_products(deal_id: int) -> list:
    """Получение списка товаров/услуг (например, Мастер-классов), привязанных к сделке."""
    result = await safe_call("crm.deal.productrows.get", {"id": deal_id})
    return result if isinstance(result, list) else[]

@mcp.tool()
async def set_deal_products(deal_id: int, products: list[dict]) -> dict:
    """
    Привязка списка товаров/услуг к сделке.
    Формат:[{"PRODUCT_NAME": "МК Свидание", "PRICE": 5000, "QUANTITY": 1}]
    Внимание: это действие перезапишет текущие товары сделки!
    """
    result = await safe_call("crm.deal.productrows.set", {"id": deal_id, "rows": products})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "message": "Товары сделки успешно обновлены"}

@mcp.tool()
async def sync_deal_payment_date(deal_id: int, date_field_label: str = "Дата оплаты") -> dict:
    """Перенести дату успешной оплаты в пользовательское поле сделки."""
    fields = await safe_call("crm.deal.userfield.list", {"filter": {"LANG": "ru", "USER_TYPE_ID": "date"}})
    if not isinstance(fields, list): fields =[]

    target_field_name = next((f.get("FIELD_NAME") for f in fields if f.get("EDIT_FORM_LABEL") == date_field_label), None)
    if not target_field_name: return {"error": f"Поле '{date_field_label}' не найдено"}

    payments_data = await safe_call("crm.item.payment.list", {"entityId": deal_id, "entityTypeId": 2})
    payments = payments_data.get("payments",[]) if isinstance(payments_data, dict) else payments_data
    if not payments or not isinstance(payments, list): return {"error": "Оплаты не найдены"}

    date_paid = payments[0].get("datePaid")
    if not date_paid: return {"error": "У найденной оплаты отсутствует дата"}

    res = await safe_call("crm.deal.update", {"ID": deal_id, "fields": {target_field_name: date_paid}})
    return {"success": bool(res), "date_paid": date_paid}

@mcp.tool()
async def get_crm_stages(entity_id: str) -> list:
    """Получить список стадий с семантикой для объектов CRM (например: 'DEAL_STAGE')."""
    return await safe_call("crm.status.list", {"filter": {"ENTITY_ID": entity_id}, "order": {"SORT": "ASC"}})

@mcp.tool()
async def get_deal_funnels_and_stages() -> dict:
    """Получить все воронки сделок и их стадии с семантикой."""
    categories = await safe_call("crm.dealcategory.list")
    if not isinstance(categories, list): categories =[]

    default_cat = await safe_call("crm.dealcategory.default.get")
    if isinstance(default_cat, dict) and "error" not in default_cat:
        categories.append(default_cat)

    result = {}
    for cat in categories:
        cat_id = cat.get("ID")
        cat_name = cat.get("NAME")
        entity_id = f"DEAL_STAGE_{cat_id}" if int(cat_id) > 0 else "DEAL_STAGE"
        stages = await safe_call("crm.status.list", {"filter": {"ENTITY_ID": entity_id}})
        result[cat_name] = stages if isinstance(stages, list) else[]
    return result


# ==========================================
# СЧЕТА И ФИНАНСЫ (Smart Invoices)
# ==========================================

@mcp.tool()
async def create_smart_invoice(title: str, contact_id: int, opportunity: float, deal_id: int = None) -> dict:
    """
    Создание нового счета (Smart Invoice, entityTypeId=31).
    Используется для выставления счетов клиентам на оплату услуг.
    """
    fields = {"title": title, "contactId": contact_id, "opportunity": opportunity}
    if deal_id: fields["parentId2"] = deal_id # Привязка к сделке
    result = await safe_call("crm.item.add", {"entityTypeId": 31, "fields": fields})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "invoice_id": result.get("item", {}).get("id") if isinstance(result, dict) else None}


# ==========================================
# КАЛЕНДАРИ И БРОНИРОВАНИЕ РЕСУРСОВ
# ==========================================

@mcp.tool()
async def check_calendar_availability(user_ids: list[int], date_from: str, date_to: str) -> dict:
    """
    Проверка занятости сотрудников или ресурсов (комнат/студий).
    Формат дат: YYYY-MM-DD. Возвращает список занятых слотов.
    """
    params = {"users": user_ids, "from": date_from, "to": date_to}
    result = await safe_call("calendar.accessibility.get", params)
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "busy_slots": result}

@mcp.tool()
async def add_calendar_event(user_id: int, name: str, date_from: str, date_to: str, description: str = "") -> dict:
    """
    Создание события в основном календаре (бронирование времени/студии).
    Формат дат: YYYY-MM-DD HH:MM:SS (В часовом поясе портала).
    """
    params = {"type": "user", "ownerId": user_id, "name": name, "description": description, "from": date_from, "to": date_to, "skipTime": "N"}
    result = await safe_call("calendar.event.add", params)
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "event_id": result}


# ==========================================
# АКТИВНОСТИ И КОММУНИКАЦИИ
# ==========================================

@mcp.tool()
async def get_deal_activities(assigned_by_id: int = None) -> list:
    """Получить список незавершенных дел из сделок."""
    filter_params = {}
    if assigned_by_id: filter_params["assignedById"] = assigned_by_id

    deals = await safe_call("crm.item.list", {"entityTypeId": 2, "select": ["id", "title"], "filter": filter_params})
    if isinstance(deals, dict) and "items" in deals: deals = deals["items"]
    if not deals or not isinstance(deals, list): return []

    bindings =[{"OWNER_TYPE_ID": 2, "OWNER_ID": d["id"]} for d in deals]
    activities = await safe_call("crm.activity.list", {"filter": {"BINDINGS": bindings, "COMPLETED": "N"}, "select":["ID", "OWNER_ID", "SUBJECT", "DEADLINE", "RESPONSIBLE_ID"]})
    return activities if isinstance(activities, list) else[]

@mcp.tool()
async def add_todo_activity(owner_type_id: int, owner_id: int, title: str, description: str, deadline_hours: int = 1) -> dict:
    """Создать дело (звонок/напоминание) в CRM."""
    deadline = (datetime.now() + timedelta(hours=deadline_hours)).strftime("%Y-%m-%d %H:%M:%S")
    params = {"ownerTypeId": owner_type_id, "ownerId": owner_id, "title": title, "description": description, "deadline": deadline}
    result = await safe_call("crm.activity.todo.add", params)
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "activity_id": result}

@mcp.tool()
async def move_activity(activity_id: int, source_type_id: int, source_id: int, target_type_id: int, target_id: int) -> dict:
    """Перенести дело из одной сущности CRM в другую."""
    if source_type_id == target_type_id:
        params = {"activityId": activity_id, "sourceEntityTypeId": source_type_id, "sourceEntityId": source_id, "targetEntityTypeId": target_type_id, "targetEntityId": target_id}
        result = await safe_call("crm.activity.binding.move", params)
    else:
        await safe_call("crm.activity.binding.add", {"activityId": activity_id, "entityTypeId": target_type_id, "entityId": target_id})
        await safe_call("crm.activity.binding.delete", {"activityId": activity_id, "entityTypeId": source_type_id, "entityId": source_id})
        result = True
    return {"success": bool(result), "message": "Дело перенесено"}

@mcp.tool()
async def send_contact_email(contact_id: int, subject: str, message: str) -> dict:
    """Отправить Email клиенту прямо из CRM."""
    contact = await safe_call("crm.contact.get", {"ID": contact_id})
    if not contact or "error" in contact: return {"error": "Контакт не найден"}

    contact_email = contact.get("EMAIL", [{}])[0].get("VALUE", "") if contact.get("EMAIL") else ""
    if not contact_email: return {"error": "У контакта нет email"}

    users = await safe_call("user.get", {"filter": {"ID": contact.get("ASSIGNED_BY_ID")}})
    if not users: return {"error": "Сотрудник не найден"}
    staff, staff_email = users[0], users[0].get("EMAIL")
    if not staff_email: return {"error": "У сотрудника нет email"}

    start_time = datetime.now()
    fields = {
        "SUBJECT": subject, "DESCRIPTION": message, "DESCRIPTION_TYPE": 3, "COMPLETED": "Y", "DIRECTION": 2, "OWNER_ID": contact_id, "OWNER_TYPE_ID": 3, "TYPE_ID": 4,
        "START_TIME": start_time.strftime("%Y-%m-%dT%H:%M:%S"), "END_TIME": (start_time + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
        "RESPONSIBLE_ID": staff.get("ID"), "COMMUNICATIONS":[{"VALUE": contact_email, "ENTITY_ID": contact_id, "ENTITY_TYPE_ID": 3}],
        "SETTINGS": {"MESSAGE_FROM": f"{staff.get('NAME', '')} {staff.get('LAST_NAME', '')} <{staff_email}>".strip()}
    }
    result = await safe_call("crm.activity.add", {"fields": fields})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "activity_id": result}

@mcp.tool()
async def add_contact_calendar_event(contact_id: int, subject: str, description: str, duration_hours: int = 1) -> dict:
    """Добавить встречу (событие CRM) в карточку контакта."""
    contact = await safe_call("crm.contact.get", {"ID": contact_id})
    if isinstance(contact, dict) and "error" in contact: return {"error": f"Контакт не найден: {contact['error']}"}

    start_time = datetime.now()
    end_time = start_time + timedelta(hours=duration_hours)
    fields = {
        "OWNER_TYPE_ID": 3, "OWNER_ID": contact_id, "TYPE_ID": 1, "SUBJECT": subject, "DESCRIPTION": description, "DESCRIPTION_TYPE": 3,
        "START_TIME": start_time.strftime("%Y-%m-%dT%H:%M:%S"), "END_TIME": end_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "RESPONSIBLE_ID": contact.get("ASSIGNED_BY_ID"), "COMMUNICATIONS":[{"ENTITY_ID": contact_id, "ENTITY_TYPE_ID": 3}]
    }
    result = await safe_call("crm.activity.add", {"fields": fields})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "activity_id": result}


# ==========================================
# ЗАДАЧИ, ТАЙМЛАЙН, УВЕДОМЛЕНИЯ
# ==========================================

@mcp.tool()
async def get_tasks(limit: int = 20) -> list:
    """Получение списка активных задач на портале."""
    limit = max(1, min(limit, 50))
    tasks = await safe_call("tasks.task.list", {"filter": {"<STATUS": 5}, "select": ["ID", "TITLE", "DEADLINE", "STATUS"]})
    if isinstance(tasks, dict) and "tasks" in tasks: return tasks["tasks"][:limit]
    return tasks[:limit] if isinstance(tasks, list) else[]

@mcp.tool()
async def add_task(title: str, responsible_id: int, description: str = "", deadline: str = "") -> dict:
    """Постановка новой задачи сотруднику. deadline: YYYY-MM-DD HH:MM:SS."""
    fields = {"TITLE": title, "RESPONSIBLE_ID": responsible_id, "DESCRIPTION": description}
    if deadline: fields["DEADLINE"] = deadline
    result = await safe_call("tasks.task.add", {"fields": fields})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "task_id": result.get("task", {}).get("id") if isinstance(result, dict) else None}

@mcp.tool()
async def get_timeline_comments(entity_type: str, entity_id: int) -> list:
    """Чтение комментариев (истории) из карточки CRM (lead, deal, contact)."""
    params = {"filter": {"ENTITY_ID": entity_id, "ENTITY_TYPE": entity_type.upper()}, "select": ["ID", "COMMENT", "CREATED"]}
    result = await safe_call("crm.timeline.comment.list", params)
    return result if isinstance(result, list) else[]

@mcp.tool()
async def send_chat_notification(user_id: int, message: str) -> dict:
    """Отправка уведомления администратору прямо в чат Битрикс24."""
    result = await safe_call("im.notify.personal.add", {"USER_ID": user_id, "MESSAGE": message})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "notification_id": result}

@mcp.tool()
async def get_users(search_name: str = "") -> list:
    """Поиск сотрудников (для передачи ID ответственного)."""
    filter_params = {"ACTIVE": "Y"}
    if search_name: filter_params["NAME"] = search_name
    result = await safe_call("user.get", {"filter": filter_params})
    return result if isinstance(result, list) else[]


# ==========================================
# СМАРТ-ПРОЦЕССЫ, ДОКУМЕНТЫ, ФАЙЛЫ, БП
# ==========================================

@mcp.tool()
async def create_crm_document(deal_id: int, template_id: int) -> dict:
    """Генерация документа (договор/счет) по шаблону."""
    res = await safe_call("crm.documentgenerator.document.add", {"templateId": template_id, "entityTypeId": 2, "entityId": deal_id})
    if not res or (isinstance(res, dict) and "error" in res): return {"error": "Не удалось сгенерировать документ."}
    doc = res.get("document", {})
    return {"success": True, "document_id": doc.get("id"), "download_url": doc.get("downloadUrl")}

@mcp.tool()
async def add_smart_process_comment(spa_title: str, element_id: int, comment: str) -> dict:
    """Добавить комментарий в элемент смарт-процесса."""
    types_data = await safe_call("crm.type.list", {"filter": {"title": spa_title}})
    types = types_data.get("types", []) if isinstance(types_data, dict) else[]
    if not types: return {"error": "Смарт-процесс не найден"}

    entity_type_id = types[0].get("entityTypeId")
    fields = {"ENTITY_ID": element_id, "ENTITY_TYPE": f"DYNAMIC_{entity_type_id}", "COMMENT": comment}
    result = await safe_call("crm.timeline.comment.add", {"fields": fields})
    return {"success": True, "comment_id": result}

@mcp.tool()
async def add_smart_process_userfield(spa_title: str, field_name_suffix: str, ru_label: str) -> dict:
    """Создать пользовательское поле в смарт-процессе."""
    types_data = await safe_call("crm.type.list", {"filter": {"title": spa_title}})
    types = types_data.get("types",[]) if isinstance(types_data, dict) else[]
    if not types: return {"error": "Смарт-процесс не найден"}

    spa_id = types[0].get("id")
    field = {"entityId": f"CRM_{spa_id}", "fieldName": f"UF_CRM_{spa_id}_{field_name_suffix}", "userTypeId": "enumeration", "multiple": "Y", "editFormLabel": {"ru": ru_label}, "enum":[{"value": "Вариант 1", "def": "N", "sort": 100}, {"value": "Вариант 2", "def": "Y", "sort": 200}]}
    result = await safe_call("userfieldconfig.add", {"moduleId": "crm", "field": field})
    return {"success": True, "result": result}

@mcp.tool()
async def create_smart_process_funnel(spa_title: str, funnel_name: str) -> dict:
    """Создать новую воронку в смарт-процессе."""
    types_data = await safe_call("crm.type.list", {"filter": {"title": spa_title}})
    types = types_data.get("types",[]) if isinstance(types_data, dict) else[]
    if not types: return {"error": "Смарт-процесс не найден"}

    entity_type_id = types[0].get("entityTypeId")
    res = await safe_call("crm.category.add", {"entityTypeId": entity_type_id, "fields": {"name": funnel_name, "sort": 100}})
    category_id = res.get("category", {}).get("id") if isinstance(res, dict) else None
    return {"success": True, "category_id": category_id}

@mcp.tool()
async def start_bizproc(template_id: int, document_type: str, document_id: str, parameters: dict = None) -> dict:
    """Запуск бизнес-процесса. document_type: 'crm', document_id: 'DEAL_123'"""
    params = {"TEMPLATE_ID": template_id, "DOCUMENT_ID":["crm", "CCrmDocumentDeal" if "DEAL" in document_id else "CCrmDocumentLead", document_id], "PARAMETERS": parameters or {}}
    result = await safe_call("bizproc.workflow.start", params)
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "workflow_id": result}

@mcp.tool()
async def upload_file_to_disk(folder_id: int, file_name: str, base64_content: str) -> dict:
    """Загрузка файла (например, чека об оплате) на Диск. Требуется Base64."""
    params = {"id": folder_id, "data": {"NAME": file_name}, "fileContent": [file_name, base64_content]}
    result = await safe_call("disk.folder.uploadfile", params)
    if isinstance(result, dict) and "error" in result: return result
    return {"success": True, "file_info": result}


# ==========================================
# ОБЩИЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

@mcp.tool()
async def update_crm_element(entity_type: str, entity_id: int, fields: dict) -> dict:
    """Универсальное обновление любого элемента CRM (lead, deal, contact)."""
    method = f"crm.{entity_type}.update".lower()
    result = await safe_call(method, {"ID": entity_id, "fields": fields})
    if isinstance(result, dict) and "error" in result: return result
    return {"success": bool(result), "message": "Успешно обновлено!" if result else "Не удалось обновить."}

@mcp.tool()
async def find_duplicates_by_comm(phone: str = "", email: str = "") -> dict:
    """Поиск дубликатов по контактам, лидам, компаниям."""
    if not phone and not email: return {"error": "Необходимо указать телефон или email"}
    all_duplicates = {}
    if phone:
        phone_dupes = await safe_call("crm.duplicate.findbycomm", {"type": "PHONE", "values": [phone]})
        if isinstance(phone_dupes, dict): all_duplicates.update(phone_dupes)
    if email:
        email_dupes = await safe_call("crm.duplicate.findbycomm", {"type": "EMAIL", "values": [email]})
        if isinstance(email_dupes, dict):
            for key, value in email_dupes.items():
                if key in all_duplicates:
                    all_duplicates[key] = list(set(all_duplicates[key] + value))
                else:
                    all_duplicates[key] = value
    return all_duplicates

@mcp.tool()
async def get_items_by_stage_name(entity_type_id: int, stage_name: str, category_id: int = 0) -> list:
    """Получить элементы (сделки, смарт-процессы) отфильтрованные по текстовому названию стадии."""
    entity_id_status = f"DEAL_STAGE_{category_id}" if category_id > 0 else "DEAL_STAGE"
    if entity_type_id != 2: entity_id_status = f"DYNAMIC_{entity_type_id}_STAGE_{category_id}"

    statuses = await safe_call("crm.status.list", {"filter": {"ENTITY_ID": entity_id_status}})
    if not isinstance(statuses, list): return[]

    target_stage = next((s for s in statuses if s.get("NAME") == stage_name), None)
    if not target_stage: return [{"error": "Стадия с таким названием не найдена"}]

    stage_id = target_stage.get("STATUS_ID")
    res = await safe_call("crm.item.list", {"entityTypeId": entity_type_id, "filter": {"stageId": stage_id}})
    if isinstance(res, dict) and "items" in res: res = res["items"]
    return res if isinstance(res, list) else[]

@mcp.tool()
def today() -> str:
    """Получить текущую дату и время в формате YYYY-MM-DD HH:MM"""
    now = datetime.now()
    result = {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"), "full_datetime": now.strftime("%Y-%m-%d %H:%M")}
    return json.dumps(result)


# ==========================================
# РЕСУРСЫ (RESOURCES) ДЛЯ ПРЯМОГО ЧТЕНИЯ LLM
# ==========================================

@mcp.resource("contact://{contact_id}")
async def get_contact_resource(contact_id: str) -> str:
    """Получение данных контакта в виде читаемого текста."""
    try: c_id = int(contact_id)
    except ValueError: return f"Некорректный ID контакта: {contact_id}"

    contact = await safe_call("crm.contact.get", {"ID": c_id})
    if isinstance(contact, dict) and "error" in contact: return f"Контакт с ID={contact_id} не найден."

    lines =[
        f"Контакт ID: {contact.get('ID')}",
        f"Имя: {contact.get('NAME', '')} {contact.get('LAST_NAME', '')}",
        f"Телефоны: {[p.get('VALUE') for p in contact.get('PHONE', [])]}",
        f"Email: {[e.get('VALUE') for e in contact.get('EMAIL', [])]}",
        f"Компания ID: {contact.get('COMPANY_ID', 'Не указана')}",
        f"Ответственный ID: {contact.get('ASSIGNED_BY_ID', 'Не назначен')}",
    ]
    return "\n".join(lines)

@mcp.resource("deal://{deal_id}")
async def get_deal_resource(deal_id: str) -> str:
    """Получение данных сделки в виде читаемого текста."""
    try: d_id = int(deal_id)
    except ValueError: return f"Некорректный ID сделки: {deal_id}"

    deal = await safe_call("crm.deal.get", {"ID": d_id})
    if isinstance(deal, dict) and "error" in deal: return f"Сделка с ID={deal_id} не найдена."

    lines =[
        f"Сделка ID: {deal.get('ID')}",
        f"Название: {deal.get('TITLE')}",
        f"Стадия: {deal.get('STAGE_ID')}",
        f"Сумма: {deal.get('OPPORTUNITY')} {deal.get('CURRENCY_ID')}",
        f"Компания ID: {deal.get('COMPANY_ID', 'Не указана')}",
        f"Ответственный ID: {deal.get('ASSIGNED_BY_ID', 'Не назначен')}",
        f"Закрыта: {'Да' if deal.get('CLOSED') == 'Y' else 'Нет'}",
    ]
    return "\n".join(lines)

@mcp.resource("deals://active")
async def get_active_deals_resource() -> str:
    """Получение списка активных сделок в виде текста."""
    deals = await safe_call("crm.deal.list", {"filter": {"CLOSED": "N"}, "select":["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "CURRENCY_ID"]})
    if not deals or (isinstance(deals, dict) and "error" in deals): return "Активные сделки не найдены"

    lines = [f"Активные сделки ({len(deals)}):"]
    for i, deal in enumerate(deals, 1):
        lines.append(f"{i}. {deal.get('TITLE')} (ID: {deal.get('ID')})\n   Стадия: {deal.get('STAGE_ID')}\n   Сумма: {deal.get('OPPORTUNITY')} {deal.get('CURRENCY_ID')}\n")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()