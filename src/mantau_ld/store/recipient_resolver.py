"""Implements `mantau_core.notify.recipients.RecipientResolver` against SQLite,
and owns CRUD for the emergency-contacts table -- there's nowhere else for it
to live; the app has no server-side representation of this list yet.
"""

from __future__ import annotations

from mantau_core.notify.channels.push.tokens import DeviceToken
from mantau_core.notify.recipients import EmergencyContact

from .sync_db import SyncDatabase
from .token_store import SqliteTokenStore


class SqliteRecipientResolver:
    def __init__(self, db: SyncDatabase, token_store: SqliteTokenStore) -> None:
        self._db = db
        self._token_store = token_store

    def devices_for_camera(self, camera_id: str) -> list[DeviceToken]:
        return self._token_store.tokens_for_camera(camera_id)

    def emergency_contacts_for_camera(self, camera_id: str) -> list[EmergencyContact]:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT household_id FROM cameras WHERE camera_id=?", (camera_id,)
            ).fetchone()
        return self.list_contacts(row["household_id"]) if row and row["household_id"] else []

    def add_contact(self, household_id: str, contact: EmergencyContact) -> None:
        with self._db.lock:
            self._db.conn.execute(
                "INSERT INTO emergency_contacts(contact_id,household_id,name,phone,relation,priority) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(contact_id) DO UPDATE SET name=excluded.name, phone=excluded.phone, "
                "relation=excluded.relation,priority=excluded.priority "
                "WHERE emergency_contacts.household_id=excluded.household_id",
                (contact.contact_id, household_id, contact.name, contact.phone,
                 contact.relation, contact.priority),
            )
            self._db.conn.commit()

    def list_contacts(self, household_id: str) -> list[EmergencyContact]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT * FROM emergency_contacts WHERE household_id=? ORDER BY priority ASC",
                (household_id,),
            ).fetchall()
        return [
            EmergencyContact(contact_id=r["contact_id"], name=r["name"], phone=r["phone"],
                              relation=r["relation"], priority=r["priority"])
            for r in rows
        ]

    def delete_contact(self, household_id: str, contact_id: str) -> bool:
        with self._db.lock:
            cursor = self._db.conn.execute(
                "DELETE FROM emergency_contacts WHERE household_id=? AND contact_id=?",
                (household_id, contact_id),
            )
            self._db.conn.commit()
            return cursor.rowcount > 0
