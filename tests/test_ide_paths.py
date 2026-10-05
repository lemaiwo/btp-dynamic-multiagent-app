import pytest

from agents.ide.paths import clean_workspace_path, object_for, path_for


def test_round_trip():
    assert path_for("CLAS", "ZCL_X") == "src/CLAS/zcl_x.clas.abap"
    assert object_for("src/CLAS/zcl_x.clas.abap") == ("CLAS", "ZCL_X", None)
    assert object_for("src/CLAS/zcl_x.clas.testclasses.abap") == ("CLAS", "ZCL_X", "testclasses")
    assert object_for("src/DDLS/zi_travel.ddls.asddls") == ("DDLS", "ZI_TRAVEL", None)
    assert object_for("notes/impact.md") is None


def test_include_path():
    assert path_for("CLAS", "ZCL_X", "testclasses") == "src/CLAS/zcl_x.clas.testclasses.abap"


def test_namespaced():
    assert path_for("CLAS", "/ABC/CL_X") == "src/CLAS/#abc#cl_x.clas.abap"
    assert object_for("src/CLAS/#abc#cl_x.clas.abap") == ("CLAS", "/ABC/CL_X", None)


@pytest.mark.parametrize("t,ext", [
    ("INTF", "intf.abap"), ("PROG", "prog.abap"), ("BDEF", "bdef.asbdef"),
    ("DCLS", "dcls.asdcls"), ("DDLX", "ddlx.asddlxs"),
    ("SRVD", "srvd.srvdsrv"), ("FUNC", "func.abap"),
])
def test_types(t, ext):
    p = path_for(t, "ZX")
    assert p == f"src/{t}/zx.{ext}"
    assert object_for(p) == (t, "ZX", None)


def test_unknown_type_and_mismatch():
    with pytest.raises(ValueError):
        path_for("XXXX", "ZX")
    assert object_for("src/CLAS/zx.intf.abap") is None
    assert object_for("src/XXXX/zx.clas.abap") is None


@pytest.mark.parametrize("bad", ["../x", "/abs", "src/../../etc", "a" * 201, "", "a\nb", "a\x00b"])
def test_rejects(bad):
    with pytest.raises(ValueError):
        clean_workspace_path(bad)


def test_clean_ok():
    assert clean_workspace_path("  src/CLAS/zcl_x.clas.abap ") == "src/CLAS/zcl_x.clas.abap"
