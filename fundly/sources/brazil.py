import csv
import io
import re
import time
import zipfile
from collections import OrderedDict
from datetime import date

from .. import ids
from ..cache import normalize
from .base import Cached, Source, Inst, QuoteResult, HistoryResult, NavEntry, FUND, in_range

SCHEME = "br-cvm"
BASE = "https://dados.cvm.gov.br/dados/FI"
DAILY_URL = BASE + "/DOC/INF_DIARIO/DADOS/inf_diario_fi_{}.zip"            # monthly, YYYYMM, from 2021
YEARLY_URL = BASE + "/DOC/INF_DIARIO/DADOS/HIST/inf_diario_fi_{}.zip"      # yearly, YYYY, 2000–2020
REGISTER_URL = BASE + "/CAD/DADOS/registro_fundo_classe.zip"
FIRST_MONTHLY = 202101
TTL = 12 * 3600
# Monthly ZIPs kept in memory (~8 MB each): enough for a quote plus a 90-day history of several funds.
MAX_ZIPS = 6
ACTIVE = {"Em Funcionamento Normal", "Fase Pré-Operacional", "Em Liquidação"}
ids.register(SCHEME, re.compile(r"^\d{14}(/[A-Z0-9]+)?$").match, upper=True)

Key = tuple[str, str]  # (CNPJ digits, ID_SUBCLASSE or "")


def words_match(query: str, text: str) -> bool:
    """Every word of the query starts some word of the text (accent- and case-insensitive)."""
    words = normalize(text).split()
    return all(any(w.startswith(t) for w in words) for t in normalize(query).split())


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _csv(z: zipfile.ZipFile, name: str):
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="latin-1", newline=""), delimiter=";")


class BrazilCVMSource(Source):
    """Brazilian investment funds (incl. pension FAPI/previdência classes): official daily quota values
    published by the CVM as open data (ODbL)."""

    name = "cvm"
    search_scope = "national"
    search_hints = frozenset({"brasil", "brazil", "brazilian", "fundo", "fundos", "fic", "fia", "fim", "fif", "previdencia",
                               "multimercado", "acoes", "renda", "credito", "cotas", "investimento", "itau", "bradesco", "btg"})
    types = frozenset({FUND})
    schemes = frozenset({SCHEME})
    timeout = 120

    def __init__(self, min_interval: float | None = None):
        super().__init__(min_interval)
        # (period, key) -> (fetched_at, entries). Only the instruments actually asked for are kept.
        self._rows: dict[tuple[str, Key], tuple[float, list[NavEntry]]] = {}
        # Raw monthly ZIPs so a second fund in the same month doesn't redownload it.
        self._zips: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._register = Cached(self._load_register, TTL).get

    @staticmethod
    def _key(inst: Inst) -> Key:
        cnpj, _, sub = inst.code.partition("/")
        return cnpj, sub

    @staticmethod
    def _final(period: str) -> bool:
        # The current and previous months can still be revised; older files are final.
        today = date.today()
        if len(period) == 4:
            return int(period) < today.year
        return (today.year - int(period[:4])) * 12 + today.month - int(period[4:]) >= 2

    def _fresh(self, fetched: float, period: str) -> bool:
        return self._final(period) or time.time() - fetched < TTL

    def _download(self, period: str) -> bytes | None:
        monthly = len(period) == 6
        hit = self._zips.get(period)
        if hit and self._fresh(hit[0], period):
            return hit[1]
        data = self._fetch_file((DAILY_URL if monthly else YEARLY_URL).format(period), f"inf_diario_fi_{period}.zip")
        if data is not None and monthly:
            self._zips[period] = (time.time(), data)
            while len(self._zips) > MAX_ZIPS:
                self._zips.popitem(last=False)
        return data

    def _period(self, period: str, key: Key) -> list[NavEntry]:
        cached = self._rows.get((period, key))
        if cached and self._fresh(cached[0], period):
            return cached[1]
        data = self._download(period)
        values: dict[str, float] = {}
        c = key[0]
        formatted = f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"
        if data:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for name in z.namelist():
                    with io.TextIOWrapper(z.open(name), encoding="latin-1", newline="") as f:
                        header = f.readline().strip().split(";")
                        col = {h: i for i, h in enumerate(header)}
                        cnpj_col = col.get("CNPJ_FUNDO_CLASSE", col.get("CNPJ_FUNDO"))
                        sub_col = col.get("ID_SUBCLASSE")
                        if cnpj_col is None or "VL_QUOTA" not in col or "DT_COMPTC" not in col:
                            continue
                        for line in f:
                            # Months hold ~500k rows; a substring test before splitting keeps the scan fast.
                            if formatted not in line and c not in line:
                                continue
                            cells = line.rstrip("\r\n").split(";")
                            sub = cells[sub_col].upper() if sub_col is not None else ""
                            if _digits(cells[cnpj_col]) == c and sub == key[1] and cells[col["VL_QUOTA"]]:
                                values[cells[col["DT_COMPTC"]][:10]] = float(cells[col["VL_QUOTA"]])
        found = [NavEntry(d, v) for d, v in sorted(values.items())]
        self._rows[(period, key)] = (time.time(), found)
        return found

    @staticmethod
    def _periods(start: str, end: str) -> list[str]:
        out = []
        y, m = int(start[:4]), int(start[5:7])
        end_ym = min(int(end[:4]) * 100 + int(end[5:7]), date.today().year * 100 + date.today().month)
        while y * 100 + m <= end_ym:
            if y * 100 + m >= FIRST_MONTHLY:
                out.append(f"{y}{m:02d}")
            elif str(y) not in out:
                out.append(str(y))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out

    def get_history(self, inst: Inst, start: str, end: str) -> HistoryResult | None:
        key = self._key(inst)
        entries = [e for p in self._periods(start, end) for e in self._period(p, key)]
        entries = in_range(entries, start, end)
        return HistoryResult(entries=entries, currency="BRL", source=self.name) if entries else None

    def get_quote(self, inst: Inst) -> QuoteResult | None:
        key, today = self._key(inst), date.today()
        prev = date(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1)
        for period in (today.strftime("%Y%m"), prev.strftime("%Y%m")):
            entries = self._period(period, key)
            if entries:
                return QuoteResult(nav=entries[-1].nav, date=entries[-1].date, currency="BRL", source=self.name)
        return None

    def _load_register(self) -> dict[str, dict]:
        """code (CNPJ, or CNPJ/subclass) -> hit, for every class/fund that isn't cancelled."""
        data = self._fetch_file(REGISTER_URL, "registro_fundo_classe.zip")
        if data is None:
            return {}
        out: dict[str, dict] = {}
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            managers, classes = {}, {}
            for row in _csv(z, "registro_fundo.csv"):
                managers[row["ID_Registro_Fundo"]] = row.get("Gestor") or row.get("Administrador")
                if row["Situacao"] in ACTIVE:
                    out[_digits(row["CNPJ_Fundo"])] = self._hit(_digits(row["CNPJ_Fundo"]), row["Denominacao_Social"],
                                                                 managers[row["ID_Registro_Fundo"]], row.get("Tipo_Fundo"),
                                                                 row.get("Patrimonio_Liquido"))
            for row in _csv(z, "registro_classe.csv"):
                classes[row["ID_Registro_Classe"]] = row
                if row["Situacao"] in ACTIVE:
                    cnpj = _digits(row["CNPJ_Classe"])
                    out[cnpj] = self._hit(cnpj, row["Denominacao_Social"], managers.get(row["ID_Registro_Fundo"]),
                                          row.get("Classificacao") or row.get("Tipo_Classe"), row.get("Patrimonio_Liquido"))
            for row in _csv(z, "registro_subclasse.csv"):
                parent = classes.get(row["ID_Registro_Classe"])
                if parent and row["Situacao"] in ACTIVE:
                    code = f"{_digits(parent['CNPJ_Classe'])}/{row['ID_Subclasse'].upper()}"
                    out[code] = self._hit(code, f"{parent['Denominacao_Social']} - {row['Denominacao_Social']}",
                                          managers.get(parent["ID_Registro_Fundo"]),
                                          parent.get("Classificacao") or parent.get("Tipo_Classe"), None, subclass=True)
        return out

    @staticmethod
    def _hit(code: str, name: str, provider: str | None, category: str | None, equity: str | None,
             subclass: bool = False) -> dict:
        try:
            size = float(equity) if equity else 0.0
        except ValueError:
            size = 0.0
        return {"id": ids.canonical(SCHEME, code), "name": " ".join(name.split()), "type": FUND, "currency": "BRL",
                "provider": provider or None, "category": category or None, "_size": size, "_sub": subclass,
                "_text": normalize(f"{name} {code} brasil")}

    @staticmethod
    def _public(hit: dict) -> dict:
        return {k: v for k, v in hit.items() if not k.startswith("_")}

    def search(self, q: str) -> list[dict]:
        if not normalize(q):
            return []
        hits = [h for h in self._register().values() if not h["_sub"] and words_match(q, h["_text"])]
        hits.sort(key=lambda h: -h["_size"])
        return [self._public(h) for h in hits[:30]]

    def resolve(self, scheme: str, code: str) -> dict | None:
        if scheme != SCHEME:
            return None
        hit = self._register().get(code.upper())
        return self._public(hit) if hit else None
