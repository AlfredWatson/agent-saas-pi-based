import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1.profiles import delete_profile


class ProfileSession:
    def __init__(self, profile, referenced_session=None):
        self._responses = [profile, referenced_session]
        self.deleted = []
        self.committed = False

    async def scalar(self, _query):
        return self._responses.pop(0)

    async def delete(self, value):
        self.deleted.append(value)

    async def commit(self):
        self.committed = True


def test_delete_profile_removes_an_unreferenced_owned_profile():
    async def run():
        profile = SimpleNamespace(id=uuid4())
        db = ProfileSession(profile)
        result = await delete_profile(profile.id, SimpleNamespace(id=uuid4()), db)
        assert result is None
        assert db.deleted == [profile]
        assert db.committed is True

    asyncio.run(run())


def test_delete_profile_hides_missing_or_foreign_profile():
    async def run():
        db = ProfileSession(None)
        with pytest.raises(HTTPException) as error:
            await delete_profile(uuid4(), SimpleNamespace(id=uuid4()), db)
        assert error.value.status_code == 404
        assert error.value.detail == "profile_not_found"
        assert not db.deleted
        assert db.committed is False

    asyncio.run(run())


def test_delete_profile_rejects_a_profile_still_used_by_a_session():
    async def run():
        profile = SimpleNamespace(id=uuid4())
        db = ProfileSession(profile, referenced_session=uuid4())
        with pytest.raises(HTTPException) as error:
            await delete_profile(profile.id, SimpleNamespace(id=uuid4()), db)
        assert error.value.status_code == 409
        assert error.value.detail == "profile_in_use"
        assert not db.deleted
        assert db.committed is False

    asyncio.run(run())
