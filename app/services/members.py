from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import write_tx
from app.errors import ApiError
from app.services.rooms import PALETTE
from app.services.shared import WriteResult, bump, limits, lock_room, room_limit, validate_members


async def create(engine, settings, slug, body, acted_by=None):
    async def operation(connection):
        room = await lock_room(connection, slug)
        if acted_by is not None:
            await validate_members(connection, room["id"], {acted_by})
        count = await connection.scalar(
            text("SELECT count(*) FROM members WHERE group_id = :gid"), {"gid": room["id"]}
        )
        if count >= limits(room, settings).members:
            room_limit("members")
        version = await bump(connection, room, settings)
        color = PALETTE[count % len(PALETTE)]
        try:
            async with connection.begin_nested():
                member = await connection.scalar(
                    text("""
                    INSERT INTO members (group_id, version, name, color)
                    VALUES (:gid, :version, :name, :color) RETURNING id
                """),
                    {"gid": room["id"], "version": version, "name": body.name, "color": color},
                )
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) != "23505":
                raise
            raise ApiError(
                409, "MEMBER_NAME_TAKEN", "Name already taken", "Choose a different member name."
            ) from error
        # DECISION: Return the new member's id, name, color and committed graph version.
        return WriteResult(
            {"id": member, "name": body.name, "color": color, "graph_version": version}
        )

    return await write_tx(engine, operation)
