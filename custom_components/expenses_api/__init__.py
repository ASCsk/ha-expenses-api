import logging
import datetime
import psycopg2
import json
from homeassistant.const import EVENT_STATE_CHANGED

DOMAIN = "expenses_api"
_LOGGER = logging.getLogger(__name__)

# Database config
DB_HOST = config.get("db_host")
DB_PORT = config.get("db_port")
DB_NAME = config.get("db_name")
DB_USER = config.get("db_user")
DB_PASS = config.get("db_pass")


def setup(hass, config):
    """Set up the Expenses API integration."""

    _LOGGER.info("🚀 expenses_api loaded")

    # Connect to the database
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASS
        )
        conn.autocommit = True
    except Exception as e:
        _LOGGER.error("Failed to connect to DB: %s", e)
        return False

    # --- Helper to safely get states ---
    def safe_state(entity_id, default=None):
        s = hass.states.get(entity_id)
        return s.state if s and s.state not in (None, "unknown", "") else default

    # --- Update latest expenses ---
    def update_latest_expenses(event_time=None):
        try:
            paid_by = safe_state("input_select.filter_paid_by", "All")
            category = safe_state("input_select.filter_category", "All")
            start_date_str = safe_state("input_datetime.filter_start_date")
            end_date_str = safe_state("input_datetime.filter_end_date")

            start_date = None
            end_date = None
            if start_date_str:
                start_date = datetime.datetime.fromisoformat(start_date_str.split(" ")[0]).date()
            if end_date_str:
                end_date = datetime.datetime.fromisoformat(end_date_str.split(" ")[0]).date()

            query = "SELECT id, description, amount, paid_by, category, date FROM expenses WHERE 1=1"
            params = []

            if paid_by != "All":
                query += " AND paid_by = %s"
                params.append(paid_by)
            if category != "All":
                query += " AND category = %s"
                params.append(category)
            if start_date:
                query += " AND date >= %s"
                params.append(start_date)
            if end_date:
                query += " AND date <= %s"
                params.append(end_date)

            query += " ORDER BY date DESC, id DESC LIMIT %s"
            params.append(20)

            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall()

            expenses_list = [
                {
                    "id": r[0],
                    "description": r[1],
                    "amount": float(r[2]) if r[2] is not None else 0.0,
                    "paid_by": r[3],
                    "category": r[4],
                    "date": r[5].isoformat() if r[5] else None
                }
                for r in rows
            ]

            # Sync-safe set state
            hass.states.set(
                "expenses_api.latest_expenses",
                len(expenses_list),  # short state
                attributes={
                    "expenses": expenses_list
                }
            )

        except Exception as e:
            _LOGGER.error("Failed to update latest expenses: %s", e)

    # --- Add Expense Service ---
    def handle_add_expense(call):
        try:
            description = safe_state("input_text.expense_description", "")
            amount = float(safe_state("input_number.expense_amount", 0))
            paid_by = safe_state("input_select.expense_paid_by", "Unknown")
            category = safe_state("input_select.expense_category", "Other")
            date_str = safe_state("input_datetime.expense_date")

            date_value = None
            if date_str:
                date_value = datetime.datetime.fromisoformat(date_str.split(" ")[0]).date()

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO expenses (description, amount, paid_by, category, date)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (description, amount, paid_by, category, date_value)
                )

            _LOGGER.info("Expense added: %s %.2f by %s", description, amount, paid_by)

            # Clear inputs (use async_create_task to safely call async_set in sync context)
            hass.async_create_task(hass.states.async_set("input_text.expense_description", ""))
            hass.async_create_task(hass.states.async_set("input_number.expense_amount", 0))
            hass.async_create_task(hass.states.async_set("input_select.expense_paid_by", paid_by))
            hass.async_create_task(hass.states.async_set("input_select.expense_category", category))
            hass.async_create_task(hass.states.async_set("input_datetime.expense_date", date_str))

            # Refresh latest expenses
            update_latest_expenses()

        except Exception as e:
            _LOGGER.error("Failed to add expense: %s", e)

    # --- Initial fetch ---
    update_latest_expenses()

    # --- Listen for filter changes ---
    def state_change_listener(event):
        entity_id = event.data.get("entity_id")
        if entity_id in [
            "input_select.filter_paid_by",
            "input_select.filter_category",
            "input_datetime.filter_start_date",
            "input_datetime/filter_end_date"
        ]:
            update_latest_expenses()

    hass.bus.listen(EVENT_STATE_CHANGED, state_change_listener)


    # --- Register services ---
    hass.services.register(DOMAIN, "add_expense", handle_add_expense)
    hass.services.register(DOMAIN, "refresh_latest_expenses", lambda call: update_latest_expenses())

    # Optional loaded state
    hass.states.set(f"{DOMAIN}.loaded", "true")

    return True
