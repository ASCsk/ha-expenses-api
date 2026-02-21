import logging
import datetime
import psycopg2
import voluptuous as vol
from decimal import Decimal, ROUND_HALF_UP
from homeassistant.const import EVENT_STATE_CHANGED, EVENT_HOMEASSISTANT_STARTED, CONF_HOST, CONF_PORT, CONF_USERNAME, CONF_PASSWORD, CONF_NAME
from homeassistant.helpers import config_validation as cv

DOMAIN = "expenses_api"
_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required("db_host"): cv.string,
                vol.Required("db_port", default=5432): cv.port,
                vol.Required("db_name"): cv.string,
                vol.Required("db_user"): cv.string,
                vol.Required("db_pass"): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

def setup(hass, config):

    conf = config.get(DOMAIN, {})

    # Database config
    DB_HOST = conf.get("db_host")
    DB_PORT = conf.get("db_port")
    DB_NAME = conf.get("db_name")
    DB_USER = conf.get("db_user")
    DB_PASS = conf.get("db_pass")
    # Debug output
    print(f"[Expenses API] DB_HOST={DB_HOST}, DB_PORT={DB_PORT}, DB_NAME={DB_NAME}, DB_USER={DB_USER}, DB_PASS={'set' if DB_PASS else 'None'}")
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
            paid_by_norm = (paid_by or "All").strip().lower()
            category = safe_state("input_select.filter_category", "All")
            start_date_str = safe_state("input_datetime.filter_start_date")
            end_date_str = safe_state("input_datetime.filter_end_date")

            start_date = None
            end_date = None
            if start_date_str:
                start_date = datetime.datetime.fromisoformat(start_date_str.split(" ")[0]).date()
            if end_date_str:
                end_date = datetime.datetime.fromisoformat(end_date_str.split(" ")[0]).date()

            query = "SELECT id, date, description, category, cost, andre, helena, paid_by FROM expenses WHERE 1=1"
            params = []

            # filter by payer using signed columns: positive value indicates who paid
            if paid_by_norm != "All":
                query += " AND LOWER(paid_by) = %s"
                params.append(paid_by_norm)
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
                    "date": r[1].isoformat() if r[1] else None,
                    "description": r[2],
                    "category": r[3],
                    "cost": float(r[4]) if r[4] is not None else 0.0,
                    "andre": float(r[5]) if len(r) > 5 and r[5] is not None else 0.0,
                    "helena": float(r[6]) if len(r) > 6 and r[6] is not None else 0.0,
                    "paid_by": r[7],
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

    # --- Split helpers ---
    def get_split_percentages():
        """Return (andre_pct, helena_pct, total_pct).

        Priority:
        1. Read Home Assistant `input_number.split_andre` and `input_number.split_helena` (0-100)
        2. Fall back to hardcoded defaults (60/40)

        Returned percentages are in 0..1 range.
        """
        default_andre = 0.6
        default_helena = 0.4

        # Try Home Assistant input_numbers (0-100)
        andre_state = safe_state("input_number.split_andre", None)
        helena_state = safe_state("input_number.split_helena", None)

        andre_pct = None
        helena_pct = None

        try:
            if andre_state is not None:
                andre_pct = float(andre_state) / 100.0
        except (TypeError, ValueError):
            andre_pct = None

        try:
            if helena_state is not None:
                helena_pct = float(helena_state) / 100.0
        except (TypeError, ValueError):
            helena_pct = None

        # If HA inputs missing, use defaults
        if andre_pct is None:
            andre_pct = default_andre
        if helena_pct is None:
            helena_pct = default_helena

        total_pct = andre_pct + helena_pct
        eps = 1e-9
        if total_pct == 0:
            andre_pct, helena_pct = default_andre, default_helena
        else:
            if abs(total_pct - 1.0) > eps:
                _LOGGER.warning("Split percentages do not sum to 1 — normalizing")
            andre_pct /= total_pct
            helena_pct /= total_pct
            total_pct = 1.0
            
        return andre_pct, helena_pct, total_pct

    def compute_shares(cost, andre_pct, helena_pct, total_pct):
        """Return (andre_share, helena_share) rounded to 2 decimals and adjusted for rounding drift."""
        andre_share = (cost * Decimal(str(andre_pct / total_pct))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        helena_share = (cost * Decimal(str(helena_pct / total_pct))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        diff = cost - (andre_share + helena_share)
        if diff != 0:
            andre_share += diff

        return andre_share, helena_share

    def reset_input_fields(category, date_str):
            hass.services.call(
                "input_text",
                "set_value",
                {
                    "entity_id": "input_text.expense_description",
                    "value": ""
                },
                blocking=False
            )

            hass.services.call(
                "input_number",
                "set_value",
                {
                    "entity_id": "input_number.expense_amount",
                    "value": 0.0
                },
                blocking=False
            )
            hass.services.call(
                "input_select",
                "select_option",
                {
                    "entity_id": "input_select.expense_category",
                    "option": category
                },
                blocking=False
            )
            hass.services.call(
                "input_datetime",
                "set_datetime",
                {
                    "entity_id": "input_datetime.expense_date",
                    "datetime": date_str
                },
                blocking=False
            )

    def handle_add_expense(call):
        try:
            date_str = safe_state("input_datetime.expense_date")
            description = safe_state("input_text.expense_description", "")
            category = safe_state("input_select.expense_category", "Other")
            cost = Decimal(str(safe_state("input_number.expense_amount", 0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            paid_by = safe_state("input_select.expense_paid_by", "Unknown")
            # split: get percentages and compute shares
            andre_pct, helena_pct, total_pct = get_split_percentages()
            andre_share, helena_share = compute_shares(cost, andre_pct, helena_pct, total_pct)
            _LOGGER.debug("Computed shares: Andre=%s Helena=%s (cost=%s)", andre_share, helena_share, cost)
            

            date_value = None
            if date_str:
                date_value = datetime.datetime.fromisoformat(date_str.split(" ")[0]).date()

            paid_by_norm = (paid_by or "").strip().lower()
            andre_val = None
            helena_val = None
            if paid_by_norm == "andre":
                # Helena owes Andre her share
                helena_val = -helena_share
                andre_val = +helena_share
            elif paid_by_norm == "helena":
                # Andre owes Helena his share
                andre_val = -andre_share
                helena_val = +andre_share
            else:
                raise ValueError(f"Invalid payer selected: {paid_by!r}")
            
            andre_val = Decimal(andre_val).quantize(Decimal("0.01"))
            helena_val = Decimal(helena_val).quantize(Decimal("0.01"))

            if andre_val + helena_val != Decimal("0.00"):
                raise RuntimeError(
                    f"Ledger invariant violated: "
                    f"andre={andre_val}, helena={helena_val}"
    )

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO expenses 
                    (description, cost, category, date, andre, helena, paid_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (description, cost, category, date_value, andre_val, helena_val, paid_by_norm)
                )

            _LOGGER.info("Expense added: %s %.2f by %s", description, cost, paid_by)

            update_balances()
            update_latest_expenses()
            reset_input_fields(category, date_str)
            
            

        except Exception as e:
            _LOGGER.error("Failed to add expense: %s", e)

            hass.services.call(
                "persistent_notification",
                "create",
                {
                    "title": "Expenses API Error",
                    "message": str(e),
                    "notification_id": "expenses_api_error"
                },
            )

    # --- Initial fetch ---
    update_latest_expenses()

    # --- Listen for filter changes ---
    def state_change_listener(event):
        entity_id = event.data.get("entity_id")
        if entity_id in [
            "input_select.filter_paid_by",
            "input_select.filter_category",
            "input_datetime.filter_start_date",
            "input_datetime.filter_end_date"
        ]:
            update_latest_expenses()

    hass.bus.listen(EVENT_STATE_CHANGED, state_change_listener)


    # --- Register services ---
    hass.services.register(DOMAIN, "add_expense", handle_add_expense)
    hass.services.register(DOMAIN, "refresh_latest_expenses", lambda call: update_latest_expenses())

    # --- Balances helper ---
    def update_balances():
        """Query DB sums for `andre` and `helena`, set HA input_number states and return totals."""
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        COALESCE(SUM(andre),0),
                        COALESCE(SUM(helena),0)
                    FROM expenses
                """)
                r = cur.fetchone()

            andre_total = float(r[0] or 0.0)
            helena_total = float(r[1] or 0.0)

            net = round(andre_total, 2)

            if net > 0:
                summary = f"Helena owes Andre: €{abs(net):.2f}"
            elif net < 0:
                summary = f"Andre owes Helena: €{abs(net):.2f}"
            else:
                summary = "All settled up"

            # Sync-safe set state values
            hass.states.set("input_number.balance_andre", round(andre_total,2))
            hass.states.set("input_number.balance_nocas", round(helena_total,2))
            hass.states.set(
                f"{DOMAIN}.settlement",
                summary,
                attributes={
                    "andre_balance": andre_total,
                    "helena_balance": helena_total,
                    "net": net
                },
)

            # Also set an entity summarizing balances for quick checks
            hass.states.set(
                f"{DOMAIN}.balances",
                "ok",
                attributes={"andre": andre_total, "nocas": helena_total},
            )

            _LOGGER.debug("Balances updated: andre=%s nocas=%s", andre_total, helena_total)
            return andre_total, helena_total
        except Exception as e:
            _LOGGER.error("Failed to update balances: %s", e)
            raise

    hass.services.register(DOMAIN, "refresh_balances", lambda call: update_balances())
    hass.states.set(f"{DOMAIN}.loaded", "true")

    def on_ha_started(event):
        update_balances()

    hass.bus.listen_once(EVENT_HOMEASSISTANT_STARTED, on_ha_started)

    return True
