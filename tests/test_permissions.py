import pytest

from fos.governance.permissions import grants, is_valid_permission


def test_valid_strings():
    assert is_valid_permission("eng.project:write")
    assert is_valid_permission("*.*:*")
    assert not is_valid_permission("eng:write")
    assert not is_valid_permission("Eng.project:write")
    assert not is_valid_permission("eng.project:delete")


@pytest.mark.parametrize(
    ("granted", "required", "ok"),
    [
        ({"eng.project:write"}, "eng.project:write", True),
        ({"eng.project:write"}, "eng.project:read", False),  # write 不隐含 read，角色里显式写
        ({"eng.*:read"}, "eng.quote:read", True),
        ({"eng.*:read"}, "sales.quote:read", False),
        ({"*.*:read"}, "finance.voucher:read", True),
        ({"*.*:read"}, "finance.voucher:approve", False),
        ({"*.*:*"}, "kb.change:approve", True),
        (set(), "eng.project:read", False),
    ],
)
def test_grants(granted, required, ok):
    assert grants(frozenset(granted), required) is ok


def test_required_cannot_be_wildcard():
    with pytest.raises(ValueError):
        grants(frozenset({"*.*:*"}), "eng.*:read")
