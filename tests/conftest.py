"""pytest 公共 fixture：数据目录隔离 + config/store + fixture 文件路径。

纪律：所有测试的 SUBHUB_DATA_DIR 指向 pytest tmp_path，绝不读写仓库根的 data/。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 保证从任意工作目录运行 pytest 都能 import app（conftest 位于 tests/ 下）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app.config import AppConfig, load_config  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离的临时数据目录（tmp_path/data），并通过环境变量注入。"""
    d = tmp_path / "data"
    monkeypatch.setenv("SUBHUB_DATA_DIR", str(d))
    return d


@pytest.fixture()
def config(data_dir: Path) -> AppConfig:
    """基于临时数据目录的配置（顺带生成 token、建目录）。"""
    return load_config()


@pytest.fixture()
def store(config: AppConfig) -> Store:
    """基于临时目录的存储实例（密钥也落在临时目录）。"""
    s = Store(config.db_path, config.secret_key_path)
    yield s
    s.close()


@pytest.fixture()
def fixtures_dir() -> Path:
    """tests/fixtures 目录。"""
    return ROOT / "tests" / "fixtures"


@pytest.fixture()
def sub_a_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "sub_a.yaml"


@pytest.fixture()
def sub_b_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "sub_b.yaml"


@pytest.fixture()
def sub_uri_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "sub_uri_base64.txt"
