"""端到端自测：导入 → 确认 → 下载 → 撤回（自动换用/停供）→ 重新确认 → 重启持久化。"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

MAPPING_CSV = """展图号,展图名称,原图号
EXH-01,唐代壁画拼版,ORG-001
EXH-01,唐代壁画拼版,ORG-002
EXH-01,唐代壁画拼版,ORG-003
EXH-02,宋代山水拼版,ORG-004
"""

BATCH1_CSV = """授权号,原图号,是否允许商用,署名
LIC-001,ORG-001,是,摄影：张三
LIC-002,ORG-001,是,摄影：张三
LIC-003,ORG-002,是,摄影：李四
LIC-004,ORG-002,是,© 某图库
LIC-005,ORG-003,是,摄影：王五
LIC-006,ORG-003,否,摄影：王五
LIC-007,ORG-004,是,摄影：赵六
"""

# 含已撤回的 LIC-001（重传不得恢复）、ORG-003 的补录授权、与 LIC-004 同署名的 LIC-009
BATCH2_CSV = """授权号,原图号,是否允许商用,署名
LIC-001,ORG-001,是,摄影：张三
LIC-008,ORG-003,是,摄影：王五
LIC-009,ORG-002,是,© 某图库
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EXHIBITION_DB", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def upload(client, url, text, data=None):
    return client.post(url, files={"file": ("data.csv", text.encode("utf-8"), "text/csv")},
                       data=data or {}, follow_redirects=False)


def setup_base(client):
    assert upload(client, "/upload/mapping", MAPPING_CSV).status_code == 303
    assert upload(client, "/upload/licenses", BATCH1_CSV).status_code == 303


def status(client, ex_id):
    r = client.get(f"/api/exhibitions/{ex_id}")
    assert r.status_code == 200
    return r.json()


def license_id(client, license_no):
    for lic in client.get("/api/licenses").json():
        if lic["license_no"] == license_no:
            return lic["id"]
    raise AssertionError(f"license {license_no} not found")


def revoke(client, license_no):
    r = client.post(f"/licenses/{license_id(client, license_no)}/revoke", follow_redirects=False)
    assert r.status_code == 303
    return r


def confirm(client, ex_id, editor="王编辑", stale_data=None):
    data = stale_data or {"editor": editor}
    if stale_data is None:
        for it in status(client, ex_id)["items"]:
            if it["license_valid"]:
                data[f"license_{it['original_id']}"] = it["license_no"]
                data[f"attribution_{it['original_id']}"] = it["attribution"]
    return client.post(f"/exhibitions/{ex_id}/confirm", data=data, follow_redirects=False)


def manifest_rows(client, ex_id):
    r = client.get(f"/exhibitions/{ex_id}/manifest.csv")
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == ["原图号", "授权号", "署名"]
    return rows[1:]


# ---------------------------------------------------------------- 测试用例

def test_full_flow_import_confirm_download(client):
    setup_base(client)
    st = status(client, 1)
    assert st["exhibition_no"] == "EXH-01"
    assert st["downloadable"] is False
    assert st["missing"] == []
    assert st["unconfirmed"] == ["ORG-001", "ORG-002", "ORG-003"]

    # 未确认署名前禁止下载
    r = client.get("/exhibitions/1/manifest.csv")
    assert r.status_code == 409
    assert r.json()["unconfirmed"] == ["ORG-001", "ORG-002", "ORG-003"]

    # 编辑确认署名后可下载
    r = confirm(client, 1)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert status(client, 1)["downloadable"] is True

    rows = manifest_rows(client, 1)
    assert rows == [
        ["ORG-001", "LIC-001", "摄影：张三"],
        ["ORG-002", "LIC-003", "摄影：李四"],
        ["ORG-003", "LIC-005", "摄影：王五"],
    ]

    # 第二个展图同样走完确认流程
    assert "msg=" in confirm(client, 2).headers["location"]
    assert manifest_rows(client, 2) == [["ORG-004", "LIC-007", "摄影：赵六"]]


def test_revoke_auto_switch_same_attribution(client):
    setup_base(client)
    confirm(client, 1)
    # LIC-001 与 LIC-002 同署名（摄影：张三），撤回后自动换用，无需重新确认
    revoke(client, "LIC-001")
    st = status(client, 1)
    assert st["downloadable"] is True
    assert st["missing"] == [] and st["unconfirmed"] == []
    rows = manifest_rows(client, 1)
    assert ["ORG-001", "LIC-002", "摄影：张三"] in rows


def test_revoke_different_attribution_suspends_then_reconfirm(client):
    setup_base(client)
    confirm(client, 1)
    # LIC-003 撤回后只剩不同署名的 LIC-004 → 停供，需确认新署名
    revoke(client, "LIC-003")
    st = status(client, 1)
    assert st["downloadable"] is False
    assert st["missing"] == []
    assert st["unconfirmed"] == ["ORG-002"]
    assert client.get("/exhibitions/1/manifest.csv").status_code == 409

    # 编辑确认替代清单后恢复下载
    assert "msg=" in confirm(client, 1).headers["location"]
    assert status(client, 1)["downloadable"] is True
    assert ["ORG-002", "LIC-004", "© 某图库"] in manifest_rows(client, 1)


def test_missing_license_blocks_and_confirm_cannot_fix(client):
    setup_base(client)
    confirm(client, 1)
    # LIC-005 撤回后，ORG-003 只剩不允许商用的 LIC-006 → 缺授权
    revoke(client, "LIC-005")
    st = status(client, 1)
    assert st["downloadable"] is False
    assert st["missing"] == ["ORG-003"]
    assert st["unconfirmed"] == []
    item = [i for i in st["items"] if i["original_no"] == "ORG-003"][0]
    assert item["license_no"] is None  # 非商用授权绝不被选用

    # 缺授权时确认无效，维持停供
    r = confirm(client, 1)
    assert "err=" in r.headers["location"]
    assert status(client, 1)["downloadable"] is False
    assert client.get("/exhibitions/1/manifest.csv").status_code == 409


def test_reupload_cannot_restore_revoked_license(client):
    setup_base(client)
    confirm(client, 1)
    revoke(client, "LIC-001")  # 自动换用 LIC-002
    # 第二批含已撤回的 LIC-001：重传不得恢复
    r = upload(client, "/upload/licenses", BATCH2_CSV)
    assert r.status_code == 303
    lics = {l["license_no"]: l for l in client.get("/api/licenses").json()}
    assert lics["LIC-001"]["revoked"] == 1, "重传不能恢复已撤回的授权"
    assert lics["LIC-008"]["revoked"] == 0  # 新授权正常入库
    rows = manifest_rows(client, 1)
    assert ["ORG-001", "LIC-002", "摄影：张三"] in rows  # 仍使用替代授权


def test_interleaved_revoke_and_stale_confirm_rejected(client):
    setup_base(client)
    confirm(client, 1)
    revoke(client, "LIC-003")  # ORG-002 换用 LIC-004，待确认
    # 编辑拿到当前清单（LIC-004）准备确认
    stale = {"editor": "王编辑"}
    for it in status(client, 1)["items"]:
        if it["license_valid"]:
            stale[f"license_{it['original_id']}"] = it["license_no"]
            stale[f"attribution_{it['original_id']}"] = it["attribution"]
    # 交错发生：补录同署名授权 LIC-009，随后 LIC-004 被撤回（自动换用 LIC-009）
    upload(client, "/upload/licenses", BATCH2_CSV)
    revoke(client, "LIC-004")
    # 编辑提交的是过期清单 → 拒绝，维持停供并提示重新核对
    r = confirm(client, 1, stale_data=stale)
    assert "err=" in r.headers["location"]
    st = status(client, 1)
    assert st["downloadable"] is False
    assert st["unconfirmed"] == ["ORG-002"]
    # 重新核对当前清单后确认 → 恢复
    assert "msg=" in confirm(client, 1).headers["location"]
    assert ["ORG-002", "LIC-009", "© 某图库"] in manifest_rows(client, 1)


def test_restart_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("EXHIBITION_DB", str(tmp_path / "restart.db"))
    from app.main import app

    with TestClient(app) as c1:
        setup_base(c1)
        confirm(c1, 1)
        revoke(c1, "LIC-003")  # 停供，待确认 LIC-004
        assert status(c1, 1)["downloadable"] is False

    # 模拟服务重启：新进程/新客户端，同一 SQLite 文件
    with TestClient(app) as c2:
        st = status(c2, 1)
        assert st["downloadable"] is False
        assert st["unconfirmed"] == ["ORG-002"]
        assert c2.get("/exhibitions/1/manifest.csv").status_code == 409
        # 重启后确认替代清单，恢复下载
        assert "msg=" in confirm(c2, 1).headers["location"]
        assert ["ORG-002", "LIC-004", "© 某图库"] in manifest_rows(c2, 1)


def test_mapping_import_idempotent(client):
    setup_base(client)
    before = client.get("/api/exhibitions").json()
    upload(client, "/upload/mapping", MAPPING_CSV)  # 重复导入
    after = client.get("/api/exhibitions").json()
    assert len(before) == len(after) == 2
    assert status(client, 1)["items"].__len__() == 3


def test_pages_render(client):
    setup_base(client)
    for url in ("/", "/licenses", "/exhibitions/1"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert "展图" in r.text
