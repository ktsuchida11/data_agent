"""設定の読み込み。

- 秘密情報（APIキー）は .env から環境変数として読む。コードや設定ファイルには書かない。
- セキュリティ方針は config/security.yaml。エージェントのファイルツールからは見えない場所に置く。
- 用途ごとに違うもの（役割の文、使うツール、MCP サーバー）は、用途パック（packs/<名前>/pack.yaml）に書く。
  どのパックを使うかは、.env の AGENT_PACK で選ぶ（既定は data。ADR-0032）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from data_agent.safety.guardrails import DEFAULT_ACTIONS, Action, Guard, custom_rule

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
PACKS_DIR = PROJECT_ROOT / "packs"

# ChatAnthropic は ANTHROPIC_API_KEY を環境変数から読むため、.env を環境変数に展開しておく
load_dotenv(PROJECT_ROOT / ".env")


class Limits(BaseModel):
    max_input_chars: int = 20_000
    # 1回の依頼で呼べる回数。サブエージェントの分と、承認のあとの続きも、同じ依頼として数える（limits.py）
    max_model_calls_per_run: int = 60
    max_tool_calls_per_run: int = 120


class LoginLimits(BaseModel):
    """ログインの失敗の回数の制限（ui/login_guard.py。ADR-0038）。接続元ごとに数える。"""

    free_attempts: int = Field(default=5, ge=1)  # 続けて試せる回数。使い切ると、しばらく受け付けない
    lock_seconds: int = Field(default=60, ge=1)  # 1 回目に受け付けない時間。そのあとも試すたびに倍にする
    max_lock_seconds: int = Field(default=900, ge=1)  # 受け付けない時間の上限
    forget_after_seconds: int = Field(default=3600, ge=1)  # 最後に試してから、この時間がたったら数え直す


class ApprovalConfig(BaseModel):
    """確認（承認）の設定。エージェントが操作を実行する前に、利用者に確かめる（approvals.py）。

    削除と、メモリ（/memories/）への書き込みは、ここの設定にかかわらず、必ず確かめる。
    """

    # 新しいチャットの確認のしかた。standard: 削除・メモリへの書き込みと、ツールごとに require_approval: true と
    # したものだけ確かめる。strict: 加えて、ファイルの作成と書き換え・コードの実行・データベースの問い合わせ・Web 検索も確かめる
    default_mode: Literal["standard", "strict"] = "standard"
    allow_mode_change: bool = True  # 利用者が、画面の設定欄でチャットごとに切り替えられるようにする
    allow_remember: bool = True  # 確認のボタンに「このチャットでは以後確認しない」を出す（削除とメモリへの書き込みには出さない）
    timeout_seconds: int = Field(default=600, ge=10, le=86_400)  # 答えを待つ時間（秒）。過ぎたら、実行しない


class CustomGuardRule(BaseModel):
    """用途ごとに足す検出ルール（社員番号、案件のコードなど）。形が決まっているものだけを、正規表現で書く。"""

    model_config = ConfigDict(extra="forbid")

    kind: str  # 監査ログで使う名前（英小文字・数字・_）
    label: str  # マスクしたあとに表示する名前（「[マスク済み:社員番号]」の「社員番号」）
    pattern: str  # 正規表現（Python の書き方）
    action: Action = "mask"
    ignore_case: bool = False  # 大文字と小文字を区別しない

    @model_validator(mode="after")
    def _compiles(self) -> CustomGuardRule:
        custom_rule(self.kind, self.label, self.pattern, ignore_case=self.ignore_case)  # 誤りがあれば、ここで止める
        return self


class GuardrailConfig(BaseModel):
    enabled: bool = True
    # 種類ごとの扱い（mask / log / off）。書かなかった種類は既定の扱いになる
    actions: dict[str, Action] = Field(default_factory=lambda: dict(DEFAULT_ACTIONS))
    # 用途ごとに足す検出ルール（ADR-0032）
    custom_rules: list[CustomGuardRule] = Field(default_factory=list)

    @field_validator("actions")
    @classmethod
    def _fill_defaults(cls, value: dict[str, Action]) -> dict[str, Action]:
        unknown = set(value) - set(DEFAULT_ACTIONS)
        if unknown:
            raise ValueError(f"guardrails.actions に知らない種類があります: {sorted(unknown)}")
        return {**DEFAULT_ACTIONS, **value}

    @field_validator("custom_rules")
    @classmethod
    def _distinct_kinds(cls, value: list[CustomGuardRule]) -> list[CustomGuardRule]:
        kinds = [rule.kind for rule in value]
        if len(kinds) != len(set(kinds)):
            raise ValueError("guardrails.custom_rules の kind が重なっています")
        return value


class ScheduleLimits(BaseModel):
    max_per_user: int = 20  # 1人が登録できる予定の数
    min_interval_minutes: int = 5  # 実行の間隔の下限（短すぎる予定で料金がかさむのを防ぐ）


class SkillMiningLimits(BaseModel):
    """繰り返している作業を見つけて、スキルの下書きを作る機能の設定。"""

    enabled: bool = True
    min_requests: int = 5  # 依頼の記録がこの数に達するまでは調べない
    min_new_requests: int = 5  # 前回調べたあとに、これだけ依頼が増えたら調べ直す
    min_hours_between: int = 24  # 自動で調べる間隔の下限（時間）
    min_occurrences: int = 3  # 何回繰り返されていたら、スキルの候補にするか
    lookback_days: int = 60  # 何日前までの依頼を調べるか
    max_requests: int = 200  # 一度に調べる依頼の数の上限
    max_pending: int = 3  # 承認待ちの提案の数の上限


class CodeExecutionLimits(BaseModel):
    """Python のコードを、隔離したコンテナで実行する機能の設定。"""

    enabled: bool = True
    require_approval: bool = (
        False  # true にすると、どのチャットでも、実行のたびに利用者の承認を求める（定時実行では使えなくなる）
    )
    timeout_seconds: int = 60  # 1回の実行の時間（秒）
    max_timeout_seconds: int = 300  # モデルが延ばせる上限（秒）
    max_input_files: int = 10  # 1回の実行に渡せるファイルの数
    max_input_mb: int = 20  # 1回の実行に渡せるファイルの合計
    output_dir: str = "/outputs"  # コードが作ったファイルの保存先（作業フォルダの中）

    @field_validator("output_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        return _check_output_dir(value)


class DatabaseLimits(BaseModel):
    """データベース（PostgreSQL）からデータを読むツールの設定。接続先は .env の DATA_DB_* で決める。"""

    enabled: bool = True
    require_approval: bool = False  # true にすると、問い合わせのたびに利用者の承認を求める（定時実行では使えなくなる）
    statement_timeout_seconds: int = 30  # 1回の問い合わせの時間（秒）
    max_rows: int = 10_000  # 1回の問い合わせで受け取る行数の上限
    max_csv_mb: int = 10  # 1回の問い合わせで保存する CSV の大きさの上限
    preview_rows: int = 30  # モデルに見せる行数（全体は CSV に保存する）
    output_dir: str = "/outputs"  # 結果の CSV の保存先（作業フォルダの中）

    @field_validator("output_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        return _check_output_dir(value)


class WebSearchLimits(BaseModel):
    """Web 検索（Claude API の Web 検索ツール）の設定。検索は、1回ごとに料金がかかる。"""

    enabled: bool = True
    # 検索の前の確認（ADR-0022）。設定ファイルに書かなければ auto。
    #   auto : 質問文を調べて、外に出したくない情報らしいもの（手元のデータの写し、見張る言葉）があるときだけ確かめる
    #   True : 検索のたびに確かめる（定時実行では使えない）
    #   False: 確かめない
    require_approval: bool | Literal["auto"] = "auto"
    # 見張る言葉（顧客の名前など）の一覧。1行に1つ。プロジェクトのフォルダからの場所。ファイルがなければ使わない。
    # エージェントから読める場所（workspace/、memories/、skills/）には置けない
    watch_terms_file: str | None = "private/watch_terms.txt"
    # データベースの列（顧客の名前など）からも、見張る言葉を読む。.env に DATA_DB_SCREENER_USER があるときだけ。
    # 読む列は、そのロールに許した列（config/watch_columns.<データベース名>.txt。scripts/setup_data_db.sh）
    watch_terms_refresh_minutes: int = Field(default=10, ge=1)  # 読み直す間隔（分）
    watch_terms_max: int = Field(default=100_000, ge=1)  # 読む値の数の上限。超えたら、すべての検索を確かめる
    max_uses: int = 5  # 1回の呼び出しの中で行う検索の回数の上限
    max_calls_per_run: int = 5  # 1回の依頼で、検索のツールを呼べる回数（サブエージェントの分も含む）
    max_question_chars: int = 1_000  # 検索の依頼文の長さの上限
    max_output_chars: int = 8_000  # モデルに返す検索結果のまとめの長さの上限
    allowed_domains: list[str] = Field(default_factory=list)  # 書くと、このドメインだけを検索する
    blocked_domains: list[str] = Field(default_factory=list)  # 書くと、このドメインを検索結果から外す

    @model_validator(mode="after")
    def _one_domain_list(self) -> WebSearchLimits:
        if self.allowed_domains and self.blocked_domains:
            raise ValueError("web_search の allowed_domains と blocked_domains は、どちらか一方だけを書いてください")
        return self


class GroundingConfig(BaseModel):
    """返事の数と URL を、根拠と照合して、確かめられないものがあれば返事の下に注記を出す（ADR-0026）。返事は止めない。"""

    enabled: bool = True
    # 照合するのは、データを読んだ回の返事だけ（コードの相談で出る、ポート番号などで注記が出ないように）。
    # 次のツールを使った回か、次の拡張子のファイルを read_file で読んだ回
    tools: list[str] = Field(default_factory=lambda: ["query_database", "web_search", "run_python"])
    file_suffixes: list[str] = Field(default_factory=lambda: [".csv", ".tsv", ".json", ".jsonl", ".txt", ".md", ".log"])
    max_items: int = Field(default=10, ge=1)  # 注記に並べる数と URL の上限


class TracingConfig(BaseModel):
    """Langfuse に送った記録の扱い（ADR-0031）。"""

    # 残す日数。アプリが 1 日に 1 回、これより古い記録を消す（このアプリが送った分だけ）。0 にすると消さない
    retention_days: int = Field(default=90, ge=0)


# ---------- 用途パック（packs/<名前>/pack.yaml。ADR-0032）と MCP サーバー（ADR-0033） ----------
_SERVER_NAME = re.compile(r"[a-z][a-z0-9_]{0,19}")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")
_ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
# 鍵を入れることが多いヘッダー。値は ${環境変数名} で書く（設定ファイルに鍵を書かないため）
_SECRET_HEADER = re.compile(r"authorization|cookie|token|secret|key|password|credential", re.IGNORECASE)
Approval = bool | Literal["auto"]


class McpToolConfig(BaseModel):
    """MCP サーバーのツールのうち、使うと決めたもの 1 つの扱い。"""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None  # 確認の画面や、定時実行の説明に出す名前。書かなければツールの名前
    # 実行の前に利用者に確かめるか。true: 毎回、auto: 送る内容に、外に出したくない情報らしいものがあるときだけ、
    # false: 確かめない。既定は true（書き込みのツールを、うっかり確認なしで使わないため）
    require_approval: Approval = True
    grounding: bool = False  # 返事の数の照合で、このツールの結果を根拠のデータとして扱う（ADR-0026）
    max_calls_per_run: int | None = Field(default=None, ge=1)  # 1 回の依頼で呼べる回数
    description: str | None = None  # モデルに見せる説明。書けば、サーバーが返す説明の代わりに使う


class McpServerConfig(BaseModel):
    """つなぐ MCP サーバー 1 つ（HTTP でつなぐもの。社内の API など）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    url: str  # https://… の MCP の入口
    # 送るヘッダー。鍵は ${環境変数名} と書いて、.env に置く（例: Authorization: "Bearer ${ORDERS_API_TOKEN}"）
    headers: dict[str, str] = Field(default_factory=dict)
    ca_file: str | None = (
        None  # 社内の認証局の証明書（PEM）。プロジェクトのフォルダからの場所。書かなければ、ふつうの証明書で確かめる
    )
    allow_http: bool = False  # true にすると http:// も使える（同じ PC や、コンテナのネットワークの中だけで使う）
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    max_output_chars: int = Field(default=20_000, ge=1_000)  # モデルに返す結果の長さの上限
    # 引数を送る前に、秘密情報や個人情報らしいものがないかを確かめ、あれば送らない
    check_arguments: bool = True
    tools: dict[str, McpToolConfig]  # 使うツールだけを書く。書かなかったツールは、サーバーにあっても使わない

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError(f"url は https:// で始めてください: {value!r}")
        if parts.username or parts.password:
            raise ValueError("url に、名前やパスワードを書かないでください。鍵は headers に ${環境変数名} で書きます")
        return value

    @field_validator("headers")
    @classmethod
    def _no_literal_secrets(cls, value: dict[str, str]) -> dict[str, str]:
        for name, text in value.items():
            if _SECRET_HEADER.search(name) and not _ENV_REF.search(text):
                raise ValueError(f"ヘッダー {name} の値は、${{環境変数名}} と書いてください（鍵を設定ファイルに書かないため）")
        return value

    @field_validator("tools")
    @classmethod
    def _tool_names(cls, value: dict[str, McpToolConfig]) -> dict[str, McpToolConfig]:
        if not value:
            raise ValueError("tools に、使うツールを 1 つ以上書いてください")
        bad = [name for name in value if not _TOOL_NAME.fullmatch(name)]
        if bad:
            raise ValueError(f"tools の名前は、英数字・_・- の 40 文字以内にしてください: {bad}")
        return value

    @model_validator(mode="after")
    def _https_only(self) -> McpServerConfig:
        if urlsplit(self.url).scheme == "http" and not self.allow_http:
            raise ValueError(
                f"url が http:// です。暗号化しない通信を使うときだけ、allow_http: true を書いてください: {self.url}"
            )
        return self


class PackConfig(BaseModel):
    """用途パック（packs/<名前>/pack.yaml）。用途ごとに違うものだけを書く。守りの設定は config/security.yaml。"""

    model_config = ConfigDict(extra="forbid")

    title: str  # 用途の名前（例: データ分析エージェント）
    greeting: str  # 新しいチャットの最初のあいさつ
    prompt: str = "SYSTEM.md"  # 役割の文（システムプロンプト）。パックのフォルダからの場所
    # 組み込みのツールのうち、使うもの。それぞれ、config/security.yaml でも止められる
    builtin_tools: list[Literal["run_python", "database", "web_search"]] = Field(default_factory=list)
    # 自分で書いたツール（Python）。パックのフォルダからの場所。@agent_tool を付けた関数がツールになる
    python_tools: list[str] = Field(default_factory=list)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    @field_validator("mcp_servers")
    @classmethod
    def _server_names(cls, value: dict[str, McpServerConfig]) -> dict[str, McpServerConfig]:
        bad = [name for name in value if not _SERVER_NAME.fullmatch(name)]
        if bad:
            raise ValueError(f"mcp_servers の名前は、英小文字で始まる、英小文字・数字・_ の 20 文字以内にしてください: {bad}")
        return value


@dataclass(frozen=True)
class Pack:
    name: str
    folder: Path
    config: PackConfig

    def path(self, relative: str) -> Path:
        """パックのフォルダの中のファイル。外（../ など）は指せない。"""
        target = (self.folder / relative).resolve()
        if not target.is_relative_to(self.folder.resolve()):
            raise ValueError(f"パック {self.name} の外のファイルは使えません: {relative}")
        return target

    def system_prompt(self) -> str:
        return self.path(self.config.prompt).read_text(encoding="utf-8")


def load_pack(name: str) -> Pack:
    """用途パックを読む。誤りがあれば、どこが違うかを日本語で書いた RuntimeError。"""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
        raise RuntimeError(f"AGENT_PACK は、英小文字・数字・_ の名前にしてください: {name!r}")
    folder = PACKS_DIR / name
    config_file = folder / "pack.yaml"
    if not config_file.is_file():
        available = sorted(found.parent.name for found in PACKS_DIR.glob("*/pack.yaml"))
        raise RuntimeError(f"用途パック {name} がありません（{config_file}）。あるもの: {'、'.join(available) or 'なし'}")
    try:
        config = PackConfig.model_validate(yaml.safe_load(config_file.read_text(encoding="utf-8")) or {})
    except ValidationError as exc:
        raise RuntimeError(f"packs/{name}/pack.yaml に誤りがあります:\n{exc}") from exc
    pack = Pack(name, folder, config)
    for relative in (config.prompt, *config.python_tools):
        try:
            found = pack.path(relative).is_file()
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if not found:
            raise RuntimeError(f"packs/{name}/pack.yaml に書いたファイルがありません: {relative}")
    return pack


def _check_output_dir(value: str) -> str:
    if not value.startswith("/") or ".." in value or value.rstrip("/") == "":
        raise ValueError("output_dir は、/outputs のように / で始まるフォルダにしてください")
    return value


class SecurityConfig(BaseModel):
    limits: Limits = Field(default_factory=Limits)
    login: LoginLimits = Field(default_factory=LoginLimits)
    approvals: ApprovalConfig = Field(default_factory=ApprovalConfig)
    code_execution: CodeExecutionLimits = Field(default_factory=CodeExecutionLimits)
    database: DatabaseLimits = Field(default_factory=DatabaseLimits)
    web_search: WebSearchLimits = Field(default_factory=WebSearchLimits)
    guardrails: GuardrailConfig = Field(default_factory=GuardrailConfig)
    schedules: ScheduleLimits = Field(default_factory=ScheduleLimits)
    skill_mining: SkillMiningLimits = Field(default_factory=SkillMiningLimits)
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # 使う用途パック（packs/<名前>/）。役割の文・使うツール・MCP サーバーが、パックごとに変わる（ADR-0032）
    agent_pack: str = "data"

    # メイン（コーディング）と軽量（サブエージェント・ガード判定）のモデル
    main_model: str = "claude-opus-5-5"
    fast_model: str = "claude-haiku-5-5"
    main_effort: str = "high"  # low | medium | high | xhigh | max
    # 安全分類器で断られたとき、サーバー側で別モデルに切り替えて続ける（対応するモデルだけ。agent.py を参照）
    enable_refusal_fallback: bool = True
    # 画面の設定欄で選べるモデル（カンマ区切り）。新しいチャットは MAIN_MODEL で始まる
    model_choices: str = "claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5"

    def selectable_models(self) -> list[str]:
        """設定欄に出すモデル。MAIN_MODEL が入っていなければ、先頭に足す。"""
        choices = [name.strip() for name in self.model_choices.split(",") if name.strip()]
        return choices if self.main_model in choices else [self.main_model, *choices]

    workspace_dir: Path = PROJECT_ROOT / "workspace"
    skills_dir: Path = PROJECT_ROOT / "skills"  # 手順書の置き場所。承認したスキルも、ここに書き出す
    log_dir: Path = PROJECT_ROOT / "logs"

    # 会話の保存先（PostgreSQL）。DB_HOST が未設定なら、メモリ上に保存する（再起動で消える）
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "coding_agent"
    db_user: str = "coding_agent"
    db_password: SecretStr | None = None
    db_sslmode: str = "prefer"  # AWS の RDS では require にする
    db_pool_max: int = 5  # 同時に開く接続の上限

    # コード実行（sandbox/ のコンテナ）。SANDBOX_URL が未設定なら、コード実行のツールは付けない
    sandbox_url: str | None = None
    sandbox_token: SecretStr | None = None  # 実行環境の側に SANDBOX_TOKEN を設定したときだけ必要

    # データを読みに行くデータベース（PostgreSQL）。DATA_DB_HOST が未設定なら、データベースのツールは付けない。
    # 会話の保存先（上の DB_*）とは別のもの。読み取りだけを許したロールでつなぐ（scripts/setup_data_db.sh）
    data_db_host: str | None = None
    data_db_port: int = 5432
    data_db_name: str | None = None
    data_db_user: str | None = None
    data_db_password: SecretStr | None = None
    data_db_sslmode: str = "prefer"  # AWS の RDS では require にする
    # Web 検索の前の確認（ADR-0022）で、見張る言葉（顧客の名前など）を読むロール。未設定なら、データベースからは読まない。
    # つなぐ先は上と同じ。エージェントのロールとは別のロールにする（scripts/setup_data_db.sh が作る）
    data_db_screener_user: str | None = None
    data_db_screener_password: SecretStr | None = None

    # Web 検索に使うモデル。検索して要点をまとめる役で、チャットのモデルとは別に呼び出す
    web_search_model: str = "claude-sonnet-5-5"

    # 定時実行（会話を PostgreSQL に保存しているときだけ使える）
    schedule_timezone: str = "Asia/Tokyo"  # cron 式の時刻を、どのタイムゾーンで解釈するか
    scheduler_enabled: bool = True  # アプリの中で時刻を見て実行するか。外から起動する構成では false にする
    scheduler_poll_seconds: int = 30  # 実行の時刻が来た予定がないかを見る間隔


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_security() -> SecurityConfig:
    return SecurityConfig.model_validate(_load_yaml("security.yaml"))


@lru_cache
def get_pack() -> Pack:
    return load_pack(get_settings().agent_pack)


@lru_cache
def get_guard() -> Guard:
    config = get_security().guardrails
    custom = [(custom_rule(r.kind, r.label, r.pattern, ignore_case=r.ignore_case), r.action) for r in config.custom_rules]
    return Guard(config.actions, enabled=config.enabled, custom=custom)
