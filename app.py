import sqlite3
from datetime import date, datetime
from typing import Optional

import pandas as pd
import streamlit as st

DB_PATH = "agpm.db"
MONTHLY_CONTRIBUTION = 10.0


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS membres (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            prenom TEXT NOT NULL,
            telephone TEXT NOT NULL DEFAULT '',
            adresse TEXT NOT NULL DEFAULT '',
            prefecture TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            date_inscription TEXT NOT NULL,
            actif INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_member_identity
        ON membres(nom, prenom, telephone);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            membre_id INTEGER NOT NULL,
            montant REAL NOT NULL CHECK (montant > 0),
            date TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(membre_id) REFERENCES membres(id) ON DELETE CASCADE
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS depenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            montant REAL NOT NULL CHECK (montant > 0),
            date TEXT NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reports_membres (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            membre_id INTEGER NOT NULL,
            annee INTEGER NOT NULL,
            montant_du REAL NOT NULL DEFAULT 0 CHECK (montant_du >= 0),
            UNIQUE(membre_id, annee),
            FOREIGN KEY(membre_id) REFERENCES membres(id) ON DELETE CASCADE
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reports_association (
            annee INTEGER PRIMARY KEY,
            solde_reporte REAL NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()


def to_iso(value: date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


def fetch_df(conn: sqlite3.Connection, query: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=params)


def month_diff_inclusive(start_date: date, end_date: date) -> int:
    if end_date < start_date:
        return 0
    return (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1


def expected_months_for_member(inscription: date, year: int) -> int:
    start = max(inscription, date(year, 1, 1))
    end = date(year, 12, 31)
    today = date.today()
    if year == today.year:
        end = min(end, today)
    if year > today.year:
        return 0
    return month_diff_inclusive(start, end)


def get_association_report(conn: sqlite3.Connection, year: int) -> float:
    row = conn.execute(
        "SELECT solde_reporte FROM reports_association WHERE annee = ?",
        (year,),
    ).fetchone()
    return float(row["solde_reporte"]) if row else 0.0


def upsert_association_report(conn: sqlite3.Connection, year: int, amount: float) -> None:
    conn.execute(
        """
        INSERT INTO reports_association(annee, solde_reporte)
        VALUES(?, ?)
        ON CONFLICT(annee) DO UPDATE SET solde_reporte = excluded.solde_reporte;
        """,
        (year, amount),
    )
    conn.commit()


def upsert_member_report(conn: sqlite3.Connection, member_id: int, year: int, amount_due: float) -> None:
    conn.execute(
        """
        INSERT INTO reports_membres(membre_id, annee, montant_du)
        VALUES(?, ?, ?)
        ON CONFLICT(membre_id, annee) DO UPDATE SET montant_du = excluded.montant_du;
        """,
        (member_id, year, amount_due),
    )
    conn.commit()


def total_contributions(conn: sqlite3.Connection, year: Optional[int] = None) -> float:
    if year is None:
        row = conn.execute("SELECT COALESCE(SUM(montant), 0) AS total FROM contributions").fetchone()
    else:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(montant), 0) AS total
            FROM contributions
            WHERE strftime('%Y', date) = ?
            """,
            (str(year),),
        ).fetchone()
    return float(row["total"])


def total_expenses(conn: sqlite3.Connection, year: Optional[int] = None) -> float:
    if year is None:
        row = conn.execute("SELECT COALESCE(SUM(montant), 0) AS total FROM depenses").fetchone()
    else:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(montant), 0) AS total
            FROM depenses
            WHERE strftime('%Y', date) = ?
            """,
            (str(year),),
        ).fetchone()
    return float(row["total"])


def get_members_status(conn: sqlite3.Connection, year: int) -> pd.DataFrame:
    members = fetch_df(
        conn,
        """
        SELECT id, nom, prenom, telephone, email, date_inscription
        FROM membres
        WHERE actif = 1
        ORDER BY nom, prenom;
        """,
    )
    if members.empty:
        return members

    paid = fetch_df(
        conn,
        """
        SELECT membre_id, COALESCE(SUM(montant), 0) AS total_paye
        FROM contributions
        WHERE strftime('%Y', date) = ?
        GROUP BY membre_id;
        """,
        (str(year),),
    )
    carried = fetch_df(
        conn,
        """
        SELECT membre_id, montant_du
        FROM reports_membres
        WHERE annee = ?;
        """,
        (year,),
    )

    merged = members.merge(paid, how="left", left_on="id", right_on="membre_id").merge(
        carried, how="left", left_on="id", right_on="membre_id", suffixes=("", "_carry")
    )
    merged["total_paye"] = merged["total_paye"].fillna(0.0)
    merged["montant_du"] = merged["montant_du"].fillna(0.0)
    merged["date_inscription"] = pd.to_datetime(merged["date_inscription"], errors="coerce").dt.date

    expected = []
    for _, row in merged.iterrows():
        inscription = row["date_inscription"] if pd.notna(row["date_inscription"]) else date(year, 1, 1)
        months = expected_months_for_member(inscription, year)
        expected.append(months * MONTHLY_CONTRIBUTION + float(row["montant_du"]))

    merged["attendu"] = expected
    merged["reste"] = (merged["attendu"] - merged["total_paye"]).clip(lower=0)
    merged["statut"] = merged["reste"].apply(lambda x: "A jour" if abs(x) < 0.001 else "En retard")
    return merged[
        ["id", "nom", "prenom", "telephone", "email", "montant_du", "total_paye", "attendu", "reste", "statut"]
    ]


def page_membres(conn: sqlite3.Connection) -> None:
    st.subheader("Membres")
    with st.form("add_member", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            nom = st.text_input("Nom *").strip()
            prenom = st.text_input("Prénom *").strip()
            telephone = st.text_input("Téléphone").strip()
            prefecture = st.text_input("Préfecture").strip()
        with c2:
            email = st.text_input("Email").strip()
            adresse = st.text_input("Adresse").strip()
            date_inscription = st.date_input("Date d'inscription", value=date.today())

        submitted = st.form_submit_button("Ajouter")
        if submitted:
            if not nom or not prenom:
                st.error("Nom et prénom sont obligatoires.")
            elif email and ("@" not in email or "." not in email.split("@")[-1]):
                st.error("Email invalide.")
            else:
                try:
                    conn.execute(
                        """
                        INSERT INTO membres(nom, prenom, telephone, adresse, prefecture, email, date_inscription, actif)
                        VALUES(?, ?, ?, ?, ?, ?, ?, 1);
                        """,
                        (nom, prenom, telephone, adresse, prefecture, email, to_iso(date_inscription)),
                    )
                    conn.commit()
                    st.success("Membre ajouté.")
                except sqlite3.IntegrityError:
                    st.error("Ce membre existe déjà (même nom/prénom/téléphone).")

    st.markdown("### Liste des membres")
    df = fetch_df(
        conn,
        """
        SELECT id, nom, prenom, telephone, prefecture, email, adresse, date_inscription
        FROM membres
        WHERE actif = 1
        ORDER BY nom, prenom;
        """,
    )
    st.dataframe(df, use_container_width=True)

    if not df.empty:
        options = {f"{r['nom']} {r['prenom']} ({r['id']})": int(r["id"]) for _, r in df.iterrows()}
        selected = st.selectbox("Membre à archiver", list(options.keys()))
        if st.button("Archiver le membre", type="secondary"):
            conn.execute("UPDATE membres SET actif = 0 WHERE id = ?", (options[selected],))
            conn.commit()
            st.success("Membre archivé.")


def page_contributions(conn: sqlite3.Connection) -> None:
    st.subheader("Contributions")
    members = fetch_df(
        conn,
        "SELECT id, nom, prenom FROM membres WHERE actif = 1 ORDER BY nom, prenom",
    )
    if members.empty:
        st.info("Ajoute d'abord au moins un membre.")
        return

    member_options = {f"{r['nom']} {r['prenom']} ({r['id']})": int(r["id"]) for _, r in members.iterrows()}

    with st.form("add_contribution", clear_on_submit=True):
        member_label = st.selectbox("Membre", list(member_options.keys()))
        montant = st.number_input("Montant", min_value=0.01, value=MONTHLY_CONTRIBUTION, step=1.0)
        contribution_date = st.date_input("Date", value=date.today())
        note = st.text_input("Note (optionnel)")
        submitted = st.form_submit_button("Enregistrer")
        if submitted:
            conn.execute(
                "INSERT INTO contributions(membre_id, montant, date, note) VALUES(?, ?, ?, ?)",
                (member_options[member_label], float(montant), to_iso(contribution_date), note.strip()),
            )
            conn.commit()
            st.success("Contribution enregistrée.")

    st.markdown("### Historique")
    year = st.number_input("Année", min_value=2020, max_value=2100, value=date.today().year, step=1)
    hist = fetch_df(
        conn,
        """
        SELECT c.id, c.date, m.nom, m.prenom, c.montant, c.note
        FROM contributions c
        JOIN membres m ON m.id = c.membre_id
        WHERE strftime('%Y', c.date) = ?
        ORDER BY c.date DESC, c.id DESC;
        """,
        (str(year),),
    )
    st.dataframe(hist, use_container_width=True)
    st.metric("Total contributions (année)", f"{total_contributions(conn, int(year)):.2f} EUR")

    st.markdown("### Statut des membres")
    status = get_members_status(conn, int(year))
    if status.empty:
        st.info("Aucun membre actif.")
    else:
        st.dataframe(status, use_container_width=True)

    st.markdown("### Report N-1 par membre")
    c1, c2, c3 = st.columns(3)
    with c1:
        selected_member = st.selectbox("Membre", list(member_options.keys()), key="carry_member")
    with c2:
        carry_year = st.number_input("Année du report", min_value=2020, max_value=2100, value=date.today().year, step=1)
    with c3:
        carry_amount = st.number_input("Montant dû reporté", min_value=0.0, value=0.0, step=10.0)
    if st.button("Enregistrer le report membre"):
        upsert_member_report(conn, member_options[selected_member], int(carry_year), float(carry_amount))
        st.success("Report membre enregistré.")


def page_depenses(conn: sqlite3.Connection) -> None:
    st.subheader("Dépenses")
    with st.form("add_expense", clear_on_submit=True):
        description = st.text_input("Description *").strip()
        montant = st.number_input("Montant", min_value=0.01, value=1.0, step=1.0)
        depense_date = st.date_input("Date", value=date.today())
        submitted = st.form_submit_button("Ajouter")
        if submitted:
            if not description:
                st.error("Description obligatoire.")
            else:
                conn.execute(
                    "INSERT INTO depenses(description, montant, date) VALUES(?, ?, ?)",
                    (description, float(montant), to_iso(depense_date)),
                )
                conn.commit()
                st.success("Dépense enregistrée.")

    year = st.number_input("Année des dépenses", min_value=2020, max_value=2100, value=date.today().year, step=1)
    dep = fetch_df(
        conn,
        """
        SELECT id, date, description, montant
        FROM depenses
        WHERE strftime('%Y', date) = ?
        ORDER BY date DESC, id DESC;
        """,
        (str(year),),
    )
    st.dataframe(dep, use_container_width=True)
    st.metric("Total dépenses (année)", f"{total_expenses(conn, int(year)):.2f} EUR")


def page_dashboard(conn: sqlite3.Connection) -> None:
    st.subheader("Dashboard")
    year = st.number_input("Année financière", min_value=2020, max_value=2100, value=date.today().year, step=1)
    year = int(year)

    st.markdown("### Solde reporté de l'association")
    current_report = get_association_report(conn, year)
    new_report = st.number_input("Solde reporté (N-1 -> N)", value=float(current_report), step=10.0)
    if st.button("Enregistrer le solde reporté"):
        upsert_association_report(conn, year, float(new_report))
        st.success("Solde reporté association enregistré.")

    contrib = total_contributions(conn, year)
    dep = total_expenses(conn, year)
    report = get_association_report(conn, year)
    solde = report + contrib - dep

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contributions", f"{contrib:.2f} EUR")
    c2.metric("Dépenses", f"{dep:.2f} EUR")
    c3.metric("Report N-1", f"{report:.2f} EUR")
    c4.metric("Solde actuel", f"{solde:.2f} EUR")

    graph_df = pd.DataFrame(
        {
            "categorie": ["Contributions", "Dépenses", "Report N-1", "Solde"],
            "montant": [contrib, dep, report, solde],
        }
    ).set_index("categorie")
    st.bar_chart(graph_df)

    st.markdown("### Evolution mensuelle (année)")
    monthly = fetch_df(
        conn,
        """
        WITH months AS (
            SELECT '01' m UNION ALL SELECT '02' UNION ALL SELECT '03' UNION ALL SELECT '04'
            UNION ALL SELECT '05' UNION ALL SELECT '06' UNION ALL SELECT '07' UNION ALL SELECT '08'
            UNION ALL SELECT '09' UNION ALL SELECT '10' UNION ALL SELECT '11' UNION ALL SELECT '12'
        ),
        c AS (
            SELECT strftime('%m', date) m, SUM(montant) total
            FROM contributions
            WHERE strftime('%Y', date) = ?
            GROUP BY strftime('%m', date)
        ),
        d AS (
            SELECT strftime('%m', date) m, SUM(montant) total
            FROM depenses
            WHERE strftime('%Y', date) = ?
            GROUP BY strftime('%m', date)
        )
        SELECT months.m AS mois,
               COALESCE(c.total, 0) AS contributions,
               COALESCE(d.total, 0) AS depenses
        FROM months
        LEFT JOIN c ON c.m = months.m
        LEFT JOIN d ON d.m = months.m
        ORDER BY months.m;
        """,
        (str(year), str(year)),
    )
    monthly["solde_net"] = monthly["contributions"] - monthly["depenses"]
    st.line_chart(monthly.set_index("mois")[["contributions", "depenses", "solde_net"]])


def main() -> None:
    st.set_page_config(page_title="AGPM - Gestion Association", layout="wide")
    st.title("AGPM - Gestion de l'association")
    st.caption("Cotisation mensuelle de référence: 10 EUR.")

    conn = get_conn()
    init_db(conn)

    menu = st.sidebar.radio("Navigation", ["Membres", "Contributions", "Dépenses", "Dashboard"])
    st.sidebar.info("Les calculs sont basés sur les transactions réelles, pas sur des cellules Excel.")

    if menu == "Membres":
        page_membres(conn)
    elif menu == "Contributions":
        page_contributions(conn)
    elif menu == "Dépenses":
        page_depenses(conn)
    else:
        page_dashboard(conn)


if __name__ == "__main__":
    main()
