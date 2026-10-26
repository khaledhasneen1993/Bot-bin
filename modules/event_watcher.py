import requests
import re
from datetime import datetime, timezone, timedelta
from modules.state import state, state_lock, push_log

def get_upcoming_events() -> bool:
    """
    Scrapes Binance Support Announcement article lists.
    Extracts scheduled maintenance dates/times and records them in the state.
    Returns True if any high-volatility event/maintenance is registered.
    """
    url = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    payload = {
        "catalogId": 48, # Support Announcements
        "pageNo": 1,
        "pageSize": 10
    }
    
    found_maintenance = []
    has_event = False

    try:
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code == 200:
            articles = r.json().get("data", {}).get("catalogs", [{}])[0].get("articles", [])
            for art in articles:
                title = art.get("title", "")
                title_lower = title.lower()
                
                # Check keywords representing system maintenance or upgrades
                if any(x in title_lower for x in ["maintenance", "system upgrade", "hard fork", "network upgrade"]):
                    has_event = True
                    # Regex to find YYYY-MM-DD HH:MM
                    dt_match = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", title)
                    if dt_match:
                        date_str, time_str = dt_match.groups()
                        try:
                            event_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                            found_maintenance.append(event_dt.isoformat())
                            push_log(f"EVENT WATCHER: Detected scheduled maintenance starting at {event_dt.isoformat()} in article: '{title}'", "warning")
                        except Exception:
                            pass
                    else:
                        # Fallback for date-only matches: YYYY-MM-DD
                        d_match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
                        if d_match:
                            date_str = d_match.group(1)
                            try:
                                # Assume 00:00 UTC start for date-only
                                event_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                                found_maintenance.append(event_dt.isoformat())
                                push_log(f"EVENT WATCHER: Detected scheduled maintenance starting on {event_dt.date().isoformat()} in article: '{title}'", "warning")
                            except Exception:
                                pass
        
        with state_lock:
            state["scheduled_maintenance"] = found_maintenance

    except Exception as e:
        push_log(f"Event Watcher scrape warning: {e}", "warning")
        
    return has_event

def is_maintenance_impending() -> bool:
    """
    Checks if the current UTC time falls within the pre-maintenance window:
    starts 30 minutes before the event and runs for 2 hours after the start time.
    """
    with state_lock:
        scheduled = list(state.get("scheduled_maintenance", []))
    
    now = datetime.now(timezone.utc)
    for m_str in scheduled:
        try:
            m_dt = datetime.fromisoformat(m_str).replace(tzinfo=timezone.utc)
            # Suspend trading from 30 minutes before until 2 hours after
            if m_dt - timedelta(minutes=30) <= now <= m_dt + timedelta(hours=2):
                return True
        except Exception:
            pass
    return False
