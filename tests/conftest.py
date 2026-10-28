"""Offline regression tests; services and databases are isolated per test."""

import os
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_runtime = TemporaryDirectory(prefix="guikerong-v3-tests-")
_root = Path(__file__).resolve().parents[1]
os.environ["GUIKERONG_DATA_ROOT"] = _runtime.name
os.environ["GUIKERONG_MINDFORGE_CONFIG"] = ""
shutil.copy2(_root / "科技金融产品.csv", Path(_runtime.name) / "科技金融产品.csv")
_companies = _root / "公司"
if _companies.is_dir():
    shutil.copytree(_companies, Path(_runtime.name) / "公司")
else:
    # Private company documents are deliberately not distributed. CI exercises
    # the same full-document protocol using synthetic UTF-8/CRLF fixtures.
    for _name, _count in (("广西贺州市桂东电子科技有限公司", 28), ("广西汇发科技开发有限公司", 20)):
        _directory = Path(_runtime.name) / "公司" / _name
        _directory.mkdir(parents=True)
        for _index in range(_count - 1):
            (_directory / f"资料{_index:02}.md").write_bytes(
                (f"  测试夹具 {_index}，不是实际企业资料。\r\n" + "保留完整字段、空白和原始换行。\n" * 30).encode(
                    "utf-8"
                )
            )
        (_directory / "技术与研发字段.md").write_text(
            "| 专利名称、类型、摘要和申请信息 | 测试发明专利，已授权 |\n"
            "| 专利申请人、发明人和共同申请人 | 测试企业独立申请 |\n"
            "| 专利法律状态和有效性 | 测试数据：有效 |\n",
            encoding="utf-8",
        )
shutil.copytree(_root / "knowledge", Path(_runtime.name) / "knowledge")

sys.path.insert(0, str(Path(__file__).parent))


def pytest_sessionfinish():
    _runtime.cleanup()
