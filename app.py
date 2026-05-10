import calendar
import json
import re
import sqlite3
import unicodedata
from datetime import date, datetime
from io import BytesIO
from typing import Any, Optional

import pandas as pd
import streamlit as st

DB_PATH = "agpm.db"
MONTHLY_CONTRIBUTION = 10.0


def member_ref(member_id: int) -> str:
    return f"M{int(member_id):03d}"


def member_ref_label(reference: str, nom: str, prenom: str, village: str, telephone: str) -> str:
    village_safe = (village or "").strip() or "-"
    tel_safe = (telephone or "").strip() or "-"
    return f"{reference} | {nom} {prenom} | {village_safe} | {tel_safe}"


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
            village_origine TEXT NOT NULL DEFAULT '',
            adresse TEXT NOT NULL DEFAULT '',
            prefecture TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            date_inscription TEXT NOT NULL,
            actif INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    # Homonymes autorises: on ne bloque plus nom/prenom/telephone.
    cur.execute("DROP INDEX IF EXISTS idx_member_identity;")
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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS activites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            type_operation TEXT NOT NULL,
            entite TEXT NOT NULL,
            entite_id INTEGER,
            details TEXT NOT NULL
        );
        """
    )
    # Migration legere: ajoute un identifiant membre unique et stable.
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(membres)").fetchall()]
    if "reference" not in cols:
        conn.execute("ALTER TABLE membres ADD COLUMN reference TEXT;")
    if "village_origine" not in cols:
        conn.execute("ALTER TABLE membres ADD COLUMN village_origine TEXT NOT NULL DEFAULT '';")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_membres_reference ON membres(reference);")
    # Normalise toutes les references au format M007.
    conn.execute("UPDATE membres SET reference = printf('M%03d', id);")
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
    log_activity(
        conn,
        type_operation="UPDATE",
        entite="reports_association",
        entite_id=year,
        details=f"Solde reporte association annee {year} = {amount:.2f} EUR",
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
    log_activity(
        conn,
        type_operation="UPDATE",
        entite="reports_membres",
        entite_id=member_id,
        details=f"Report membre id={member_id} annee {year} = {amount_due:.2f} EUR",
    )
    conn.commit()


def log_activity(
    conn: sqlite3.Connection,
    type_operation: str,
    entite: str,
    entite_id: Optional[int],
    details: str,
) -> None:
    conn.execute(
        """
        INSERT INTO activites(created_at, type_operation, entite, entite_id, details)
        VALUES(?, ?, ?, ?, ?)
        """,
        (datetime.now().isoformat(timespec="seconds"), type_operation, entite, entite_id, details),
    )


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


def get_members_status(conn: sqlite3.Connection, year: int, include_archived: bool = False) -> pd.DataFrame:
    where_clause = "" if include_archived else "WHERE actif = 1"
    members = fetch_df(
        conn,
        """
        SELECT id, reference, nom, prenom, telephone, village_origine, email, date_inscription, actif
        FROM membres
        """
        + where_clause
        + """
        ORDER BY nom, prenom;
        """,
    )
    if members.empty:
        return pd.DataFrame(
            columns=[
                "id",
                "reference",
                "actif",
                "nom",
                "prenom",
                "telephone",
                "village_origine",
                "email",
                "montant_du",
                "total_paye",
                "attendu",
                "reste",
                "statut",
            ]
        )

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
    if "village_origine" not in merged.columns:
        merged["village_origine"] = ""
    return merged[
        [
            "id",
            "reference",
            "actif",
            "nom",
            "prenom",
            "telephone",
            "village_origine",
            "email",
            "montant_du",
            "total_paye",
            "attendu",
            "reste",
            "statut",
        ]
    ]


def page_membres(conn: sqlite3.Connection) -> None:
    st.subheader("Membres")
    with st.form("add_member", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            nom = st.text_input("Nom *").strip()
            prenom = st.text_input("Prénom *").strip()
            telephone = st.text_input("Téléphone").strip()
            village_origine = st.text_input("Village d'origine").strip()
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
                    cur = conn.execute(
                        """
                        INSERT INTO membres(reference, nom, prenom, telephone, village_origine, adresse, prefecture, email, date_inscription, actif)
                        VALUES('', ?, ?, ?, ?, ?, ?, ?, ?, 1);
                        """,
                        (nom, prenom, telephone, village_origine, adresse, prefecture, email, to_iso(date_inscription)),
                    )
                    new_id = int(cur.lastrowid)
                    conn.execute("UPDATE membres SET reference = ? WHERE id = ?", (member_ref(new_id), new_id))
                    log_activity(
                        conn,
                        type_operation="CREATE",
                        entite="membre",
                        entite_id=new_id,
                        details=(
                            f"Ajout membre ref={member_ref(new_id)} nom={nom} prenom={prenom} "
                            f"village={village_origine or '-'} tel={telephone or '-'}"
                        ),
                    )
                    conn.commit()
                    st.success("Membre ajouté.")
                except sqlite3.IntegrityError:
                    st.error("Impossible d'ajouter ce membre (integrite des donnees).")

    st.markdown("### Liste des membres")
    view_mode = st.radio(
        "Afficher",
        ["Actifs", "Archivés", "Tous"],
        horizontal=True,
    )
    where_sql = "WHERE actif = 1"
    if view_mode == "Archivés":
        where_sql = "WHERE actif = 0"
    elif view_mode == "Tous":
        where_sql = ""

    df = fetch_df(
        conn,
        """
        SELECT
            id,
            reference,
            CASE WHEN actif = 1 THEN 'Actif' ELSE 'Archive' END AS etat,
            nom, prenom, telephone, village_origine, prefecture, email, adresse, date_inscription
        FROM membres
        """
        + where_sql
        + """
        ORDER BY nom, prenom;
        """,
    )
    if not df.empty:
        df["reference_complete"] = df.apply(
            lambda r: member_ref_label(
                str(r["reference"]),
                str(r["nom"]),
                str(r["prenom"]),
                str(r["village_origine"] or ""),
                str(r["telephone"] or ""),
            ),
            axis=1,
        )
        search = st.text_input("Recherche (référence, nom, téléphone)", "").strip().lower()
        if search:
            filt = (
                df["reference_complete"].str.lower().str.contains(search, na=False)
                | df["nom"].str.lower().str.contains(search, na=False)
                | df["prenom"].str.lower().str.contains(search, na=False)
                | df["telephone"].str.lower().str.contains(search, na=False)
            )
            df = df[filt]
    st.dataframe(df, use_container_width=True)

    status_year = st.number_input(
        "Année pour statut/décompte membres",
        min_value=2020,
        max_value=2100,
        value=date.today().year,
        step=1,
        key="members_status_year",
    )
    st.markdown("### Statut et décompte des membres")
    status_df = get_members_status(conn, int(status_year), include_archived=True)
    if view_mode == "Actifs":
        status_df = status_df[status_df["actif"] == 1]
    elif view_mode == "Archivés":
        status_df = status_df[status_df["actif"] == 0]
    status_df = status_df.copy()
    status_df["etat"] = status_df["actif"].apply(lambda v: "Actif" if int(v) == 1 else "Archive")
    st.dataframe(
        status_df[
            [
                "reference",
                "etat",
                "nom",
                "prenom",
                "telephone",
                "village_origine",
                "email",
                "montant_du",
                "total_paye",
                "attendu",
                "reste",
                "statut",
            ]
        ],
        use_container_width=True,
    )

    if not df.empty:
        options = {
            (
                f"{r['reference']} | {r['nom']} {r['prenom']} | "
                f"village:{r['village_origine'] or '-'} | tel:{r['telephone'] or '-'} | etat:{r['etat']}"
            ): int(r["id"])
            for _, r in df.iterrows()
        }
        selected = st.selectbox("Membre à modifier", list(options.keys()))
        selected_id = options[selected]
        member_row = conn.execute(
            """
            SELECT id, actif, reference, nom, prenom, telephone, village_origine,
                   adresse, prefecture, email, date_inscription
            FROM membres WHERE id = ?
            """,
            (selected_id,),
        ).fetchone()

        if member_row:
            st.markdown("#### Modifier les informations du membre")
            try:
                current_inscription = datetime.fromisoformat(member_row["date_inscription"]).date()
            except (TypeError, ValueError):
                current_inscription = date.today()

            with st.form(f"edit_member_{selected_id}"):
                e1, e2 = st.columns(2)
                with e1:
                    edit_nom = st.text_input(
                        "Nom *",
                        value=member_row["nom"] or "",
                        key=f"edit_nom_{selected_id}",
                    )
                    edit_prenom = st.text_input(
                        "Prénom *",
                        value=member_row["prenom"] or "",
                        key=f"edit_prenom_{selected_id}",
                    )
                    edit_telephone = st.text_input(
                        "Téléphone",
                        value=member_row["telephone"] or "",
                        key=f"edit_tel_{selected_id}",
                    )
                    edit_village = st.text_input(
                        "Village d'origine",
                        value=member_row["village_origine"] or "",
                        key=f"edit_village_{selected_id}",
                    )
                    edit_prefecture = st.text_input(
                        "Préfecture",
                        value=member_row["prefecture"] or "",
                        key=f"edit_pref_{selected_id}",
                    )
                with e2:
                    edit_email = st.text_input(
                        "Email",
                        value=member_row["email"] or "",
                        key=f"edit_email_{selected_id}",
                    )
                    edit_adresse = st.text_input(
                        "Adresse",
                        value=member_row["adresse"] or "",
                        key=f"edit_adresse_{selected_id}",
                    )
                    edit_inscription = st.date_input(
                        "Date d'inscription",
                        value=current_inscription,
                        key=f"edit_inscription_{selected_id}",
                    )

                update_submitted = st.form_submit_button("Mettre à jour le membre")
                if update_submitted:
                    nom_v = edit_nom.strip()
                    prenom_v = edit_prenom.strip()
                    telephone_v = edit_telephone.strip()
                    village_v = edit_village.strip()
                    prefecture_v = edit_prefecture.strip()
                    email_v = edit_email.strip()
                    adresse_v = edit_adresse.strip()
                    if not nom_v or not prenom_v:
                        st.error("Nom et prénom sont obligatoires.")
                    elif email_v and ("@" not in email_v or "." not in email_v.split("@")[-1]):
                        st.error("Email invalide.")
                    else:
                        before = (
                            f"nom={member_row['nom']}, prenom={member_row['prenom']}, "
                            f"tel={member_row['telephone'] or ''}, village={member_row['village_origine'] or ''}, "
                            f"prefecture={member_row['prefecture'] or ''}, email={member_row['email'] or ''}, "
                            f"adresse={member_row['adresse'] or ''}, date_inscription={member_row['date_inscription']}"
                        )
                        conn.execute(
                            """
                            UPDATE membres
                            SET nom = ?, prenom = ?, telephone = ?, village_origine = ?,
                                adresse = ?, prefecture = ?, email = ?, date_inscription = ?
                            WHERE id = ?
                            """,
                            (
                                nom_v,
                                prenom_v,
                                telephone_v,
                                village_v,
                                adresse_v,
                                prefecture_v,
                                email_v,
                                to_iso(edit_inscription),
                                selected_id,
                            ),
                        )
                        after = (
                            f"nom={nom_v}, prenom={prenom_v}, tel={telephone_v}, "
                            f"village={village_v}, prefecture={prefecture_v}, email={email_v}, "
                            f"adresse={adresse_v}, date_inscription={to_iso(edit_inscription)}"
                        )
                        log_activity(
                            conn,
                            type_operation="UPDATE",
                            entite="membre",
                            entite_id=selected_id,
                            details=(
                                f"Maj membre ref={member_row['reference']} | "
                                f"avant: [{before}] | apres: [{after}]"
                            ),
                        )
                        conn.commit()
                        st.success("Informations du membre mises à jour.")
                        st.rerun()

        if member_row and int(member_row["actif"]) == 1:
            if st.button("Archiver le membre", type="secondary"):
                conn.execute("UPDATE membres SET actif = 0 WHERE id = ?", (selected_id,))
                log_activity(
                    conn,
                    type_operation="UPDATE",
                    entite="membre",
                    entite_id=selected_id,
                    details=f"Archivage membre ref={member_row['reference']} nom={member_row['nom']} {member_row['prenom']}",
                )
                conn.commit()
                st.success("Membre archivé.")
        if member_row and int(member_row["actif"]) == 0:
            if st.button("Réactiver le membre"):
                conn.execute("UPDATE membres SET actif = 1 WHERE id = ?", (selected_id,))
                log_activity(
                    conn,
                    type_operation="UPDATE",
                    entite="membre",
                    entite_id=selected_id,
                    details=f"Reactivation membre ref={member_row['reference']} nom={member_row['nom']} {member_row['prenom']}",
                )
                conn.commit()
                st.success("Membre réactivé.")


def page_contributions(conn: sqlite3.Connection) -> None:
    st.subheader("Contributions")
    members = fetch_df(
        conn,
        "SELECT id, reference, nom, prenom, village_origine, telephone FROM membres WHERE actif = 1 ORDER BY nom, prenom",
    )
    if members.empty:
        st.info("Ajoute d'abord au moins un membre.")
        return

    member_options = {
        (
            f"{r['reference']} | {r['nom']} {r['prenom']} | "
            f"{r['village_origine'] or '-'} | {r['telephone'] or '-'} ({r['id']})"
        ): int(r["id"])
        for _, r in members.iterrows()
    }

    with st.form("add_contribution", clear_on_submit=True):
        member_label = st.selectbox("Membre", list(member_options.keys()))
        montant = st.number_input("Montant", min_value=0.01, value=MONTHLY_CONTRIBUTION, step=1.0)
        contribution_date = st.date_input("Date", value=date.today())
        note = st.text_input("Note (optionnel)")
        submitted = st.form_submit_button("Enregistrer")
        if submitted:
            cur = conn.execute(
                "INSERT INTO contributions(membre_id, montant, date, note) VALUES(?, ?, ?, ?)",
                (member_options[member_label], float(montant), to_iso(contribution_date), note.strip()),
            )
            log_activity(
                conn,
                type_operation="CREATE",
                entite="contribution",
                entite_id=cur.lastrowid,
                details=(
                    f"Ajout cotisation id={cur.lastrowid}, membre_id={member_options[member_label]}, "
                    f"montant={float(montant):.2f} EUR, date={to_iso(contribution_date)}"
                ),
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

    if not hist.empty:
        st.markdown("### Corriger une contribution")
        row_options = {
            f"id={int(r['id'])} | {r['date']} | {r['nom']} {r['prenom']} | {float(r['montant']):.2f} EUR": int(r["id"])
            for _, r in hist.iterrows()
        }
        selected_label = st.selectbox("Contribution à corriger", list(row_options.keys()))
        selected_id = row_options[selected_label]
        row = conn.execute(
            "SELECT id, membre_id, montant, date, note FROM contributions WHERE id = ?",
            (selected_id,),
        ).fetchone()
        if row:
            reverse_members = {v: k for k, v in member_options.items()}
            default_member_label = reverse_members.get(int(row["membre_id"]), list(member_options.keys())[0])
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                edit_member = st.selectbox(
                    "Membre (édition)",
                    list(member_options.keys()),
                    index=list(member_options.keys()).index(default_member_label),
                    key=f"edit_member_{selected_id}",
                )
            with c2:
                edit_amount = st.number_input(
                    "Montant (édition)",
                    min_value=0.01,
                    value=float(row["montant"]),
                    step=1.0,
                    key=f"edit_amount_{selected_id}",
                )
            with c3:
                edit_date = st.date_input(
                    "Date (édition)",
                    value=datetime.fromisoformat(row["date"]).date(),
                    key=f"edit_date_{selected_id}",
                )
            with c4:
                edit_note = st.text_input(
                    "Note (édition)",
                    value=row["note"] or "",
                    key=f"edit_note_{selected_id}",
                )

            b1, b2 = st.columns(2)
            with b1:
                if st.button("Mettre à jour la contribution", key=f"update_contrib_{selected_id}"):
                    before = f"membre_id={row['membre_id']}, montant={float(row['montant']):.2f}, date={row['date']}, note={row['note'] or ''}"
                    conn.execute(
                        """
                        UPDATE contributions
                        SET membre_id = ?, montant = ?, date = ?, note = ?
                        WHERE id = ?
                        """,
                        (
                            member_options[edit_member],
                            float(edit_amount),
                            to_iso(edit_date),
                            edit_note.strip(),
                            selected_id,
                        ),
                    )
                    after = (
                        f"membre_id={member_options[edit_member]}, montant={float(edit_amount):.2f}, "
                        f"date={to_iso(edit_date)}, note={edit_note.strip()}"
                    )
                    log_activity(
                        conn,
                        type_operation="UPDATE",
                        entite="contribution",
                        entite_id=selected_id,
                        details=f"Maj cotisation id={selected_id} | avant: [{before}] | apres: [{after}]",
                    )
                    conn.commit()
                    st.success("Contribution mise à jour.")
            with b2:
                if st.button("Supprimer la contribution", type="secondary", key=f"delete_contrib_{selected_id}"):
                    conn.execute("DELETE FROM contributions WHERE id = ?", (selected_id,))
                    log_activity(
                        conn,
                        type_operation="DELETE",
                        entite="contribution",
                        entite_id=selected_id,
                        details=f"Suppression cotisation id={selected_id}",
                    )
                    conn.commit()
                    st.success("Contribution supprimée.")

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
                dep_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                log_activity(
                    conn,
                    type_operation="CREATE",
                    entite="depense",
                    entite_id=int(dep_id),
                    details=f"Ajout depense id={int(dep_id)}, {description}, montant={float(montant):.2f} EUR, date={to_iso(depense_date)}",
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

    if not dep.empty:
        st.markdown("### Corriger une dépense")
        dep_options = {
            f"id={int(r['id'])} | {r['date']} | {r['description']} | {float(r['montant']):.2f} EUR": int(r["id"])
            for _, r in dep.iterrows()
        }
        selected_dep_label = st.selectbox("Dépense à corriger", list(dep_options.keys()))
        selected_dep_id = dep_options[selected_dep_label]
        dep_row = conn.execute(
            "SELECT id, date, description, montant FROM depenses WHERE id = ?",
            (selected_dep_id,),
        ).fetchone()
        if dep_row:
            d1, d2, d3 = st.columns(3)
            with d1:
                edit_dep_desc = st.text_input(
                    "Description (édition)",
                    value=dep_row["description"] or "",
                    key=f"edit_dep_desc_{selected_dep_id}",
                )
            with d2:
                edit_dep_amount = st.number_input(
                    "Montant (édition)",
                    min_value=0.01,
                    value=float(dep_row["montant"]),
                    step=1.0,
                    key=f"edit_dep_amount_{selected_dep_id}",
                )
            with d3:
                edit_dep_date = st.date_input(
                    "Date (édition)",
                    value=datetime.fromisoformat(dep_row["date"]).date(),
                    key=f"edit_dep_date_{selected_dep_id}",
                )

            x1, x2 = st.columns(2)
            with x1:
                if st.button("Mettre à jour la dépense", key=f"update_dep_{selected_dep_id}"):
                    if not edit_dep_desc.strip():
                        st.error("Description obligatoire.")
                    else:
                        before = (
                            f"description={dep_row['description']}, montant={float(dep_row['montant']):.2f}, "
                            f"date={dep_row['date']}"
                        )
                        conn.execute(
                            """
                            UPDATE depenses
                            SET description = ?, montant = ?, date = ?
                            WHERE id = ?
                            """,
                            (
                                edit_dep_desc.strip(),
                                float(edit_dep_amount),
                                to_iso(edit_dep_date),
                                selected_dep_id,
                            ),
                        )
                        after = (
                            f"description={edit_dep_desc.strip()}, montant={float(edit_dep_amount):.2f}, "
                            f"date={to_iso(edit_dep_date)}"
                        )
                        log_activity(
                            conn,
                            type_operation="UPDATE",
                            entite="depense",
                            entite_id=selected_dep_id,
                            details=f"Maj depense id={selected_dep_id} | avant: [{before}] | apres: [{after}]",
                        )
                        conn.commit()
                        st.success("Dépense mise à jour.")
            with x2:
                if st.button("Supprimer la dépense", type="secondary", key=f"delete_dep_{selected_dep_id}"):
                    conn.execute("DELETE FROM depenses WHERE id = ?", (selected_dep_id,))
                    log_activity(
                        conn,
                        type_operation="DELETE",
                        entite="depense",
                        entite_id=selected_dep_id,
                        details=f"Suppression depense id={selected_dep_id}",
                    )
                    conn.commit()
                    st.success("Dépense supprimée.")


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
    report_n2 = get_association_report(conn, year - 1)
    solde = report + contrib - dep

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contributions", f"{contrib:.2f} EUR")
    c2.metric("Dépenses", f"{dep:.2f} EUR")
    c3.metric("Report N-1", f"{report:.2f} EUR")
    c4.metric("Solde actuel", f"{solde:.2f} EUR")
    n1 = st.columns(1)[0]
    n1.metric("Solde N-2", f"{report_n2:.2f} EUR")

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


def page_activite(conn: sqlite3.Connection) -> None:
    st.subheader("Activité")
    st.caption("Trace des derniers mouvements: cotisations, depenses et modifications.")
    limit = st.selectbox("Nombre de derniers mouvements", [5, 10, 20, 50, 100], index=2)
    logs = fetch_df(
        conn,
        """
        SELECT id, created_at, type_operation, entite, entite_id, details
        FROM activites
        ORDER BY id DESC
        LIMIT ?;
        """,
        (int(limit),),
    )
    if logs.empty:
        st.info("Aucun mouvement enregistre pour le moment.")
    else:
        st.dataframe(logs, use_container_width=True)


# --- Import Excel (structure classeur AGPM Association) ---------------------------------

MONTH_ORDER_FR = [
    "janvier",
    "fevrier",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "aout",
    "septembre",
    "octobre",
    "novembre",
    "decembre",
]

SOLDE_COL_RE = re.compile(r"solde\s*(\d{4})", re.IGNORECASE)
COTISATIONS_SHEET_RE = re.compile(r"^cotisations\s*(\d{4})$", re.IGNORECASE)
DEPENSES_SHEET_RE = re.compile(r"^d[ée]penses\s*(\d{4})$", re.IGNORECASE)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normalize_header(name: object) -> str:
    if pd.isna(name):
        return ""
    return _strip_accents(str(name).strip().lower())


def normalize_phone(raw: object) -> str:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    return re.sub(r"\D", "", str(raw))


def excel_member_key(nom: object, prenom: object, tel_raw: object) -> tuple[str, str, str]:
    n = str(nom).strip().lower() if pd.notna(nom) else ""
    p = str(prenom).strip().lower() if pd.notna(prenom) else ""
    return (n, p, normalize_phone(tel_raw))


# UID import : ("t", nom_l, prenom_l, tel) si téléphone dès Excel ; sinon ("r", feuille, n° ligne) pour éviter
# de fusionner deux homonymes sans numéro avant que vous complétiez le téléphone dans l'interface.
MemberImportUid = tuple[str | int, ...]


def member_import_uid(nom: object, prenom: object, tel_raw: object, sheet: str, row_num: int) -> MemberImportUid:
    n = str(nom).strip().lower() if pd.notna(nom) else ""
    p = str(prenom).strip().lower() if pd.notna(prenom) else ""
    t = normalize_phone(tel_raw)
    if t:
        return ("t", n, p, t)
    return ("r", sheet, row_num)


def uid_import_to_json(uid: MemberImportUid) -> str:
    return json.dumps(list(uid))


def uid_import_from_json(s: str) -> MemberImportUid:
    L = json.loads(s)
    kind = L[0]
    if kind == "r":
        return ("r", str(L[1]), int(L[2]))
    return ("t", str(L[1]), str(L[2]), str(L[3]))


def member_import_row_label(uid: MemberImportUid) -> str:
    if uid[0] == "r":
        return f"{uid[1]}, ligne {uid[2]}"
    return "Téléphone lu depuis Excel"


def sync_member_phones_from_editor(bundle: dict[str, Any], edited: pd.DataFrame) -> None:
    """Met à jour bundle['members'][uid]['telephone'] depuis la colonne éditée (_uid_json + telephone)."""
    if edited.empty or "_uid_json" not in edited.columns:
        return
    members = bundle["members"]
    col_tel = "telephone" if "telephone" in edited.columns else None
    if not col_tel:
        return
    for _, row in edited.iterrows():
        uid = uid_import_from_json(str(row["_uid_json"]))
        if uid not in members:
            continue
        raw_tel = row[col_tel]
        members[uid]["telephone"] = normalize_phone(raw_tel)


def collapse_bundle_members_by_phone(bundle: dict[str, Any]) -> None:
    """Fusionne les entrées ayant le même nom+prénom+téléphone (téléphone non vide après édition)."""
    members: dict[MemberImportUid, dict[str, str]] = bundle["members"]
    contributions: list[dict[str, object]] = bundle["contributions"]
    reports: list[dict[str, object]] = bundle["reports"]

    groups: dict[tuple[str, str, str], list[MemberImportUid]] = {}
    for uid, info in members.items():
        dk = excel_member_key(info["nom"], info["prenom"], info["telephone"])
        if dk[2] == "":
            continue
        groups.setdefault(dk, []).append(uid)

    uid_redirect: dict[MemberImportUid, MemberImportUid] = {uid: uid for uid in members}

    for uids in groups.values():
        if len(uids) < 2:
            continue
        canon = uids[0]
        merged = dict(members[canon])
        for u in uids[1:]:
            ou = members[u]
            merged["telephone"] = merge_member_field(merged["telephone"], ou["telephone"])
            merged["email"] = merge_member_field(merged["email"], ou["email"])
            merged["adresse"] = merge_member_field(merged["adresse"], ou["adresse"])
            merged["village_origine"] = merge_member_field(merged["village_origine"], ou["village_origine"])
            merged["prefecture"] = merge_member_field(merged["prefecture"], ou["prefecture"])
            uid_redirect[u] = canon
            del members[u]
        members[canon] = merged

    for c in contributions:
        ouid = c["member_uid"]
        c["member_uid"] = uid_redirect.get(ouid, ouid)
    for r in reports:
        ouid = r["member_uid"]
        r["member_uid"] = uid_redirect.get(ouid, ouid)


def validate_bundle_phones(bundle: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    for uid, info in bundle["members"].items():
        if not normalize_phone(info["telephone"]):
            label = f"{info['nom']} {info['prenom']}"
            if uid[0] == "r":
                label += f" ({uid[1]}, ligne {uid[2]})"
            errs.append(label)
    return errs


def parse_money_cell(val: object) -> Optional[float]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("", "x", "p", "a", "-", "np", "n/p"):
            return None
        try:
            return float(v.replace(",", "."))
        except ValueError:
            return None
    try:
        f = float(val)
        if f <= 0:
            return None
        return f
    except (TypeError, ValueError):
        return None


def parse_solde_report_amount(val: object) -> Optional[float]:
    """Convertit une cellule Solde Excel en montant_dû (>= 0) pour reports_membres."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    # Excel: solde négatif = retard à reporter
    if f < 0:
        return round(abs(f), 2)
    return 0.0


def month_columns_from_df(df: pd.DataFrame) -> dict[int, str]:
    norm_to_orig = {normalize_header(c): c for c in df.columns}
    mapping: dict[int, str] = {}
    for mi, key in enumerate(MONTH_ORDER_FR, start=1):
        if key in norm_to_orig:
            mapping[mi] = norm_to_orig[key]
    return mapping


def solde_columns_from_df(df: pd.DataFrame) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for c in df.columns:
        m = SOLDE_COL_RE.search(str(c))
        if m:
            out.append((int(m.group(1)), str(c)))
    return out


def resolve_col(df: pd.DataFrame, *candidates: str) -> Optional[str]:
    norm_to_orig = {normalize_header(c): c for c in df.columns}
    for cand in candidates:
        nn = normalize_header(cand)
        if nn in norm_to_orig:
            return norm_to_orig[nn]
    return None


def cotisation_sheet_year(name: str) -> Optional[int]:
    m = COTISATIONS_SHEET_RE.match(name.strip())
    return int(m.group(1)) if m else None


def depenses_sheet_year(name: str) -> Optional[int]:
    m = DEPENSES_SHEET_RE.match(name.strip())
    return int(m.group(1)) if m else None


def last_day_of_month(year: int, month: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last)


def merge_member_field(prev: str, new_val: object) -> str:
    if new_val is None or (isinstance(new_val, float) and pd.isna(new_val)):
        return prev
    s = str(new_val).strip()
    if not s:
        return prev
    return s if not prev else prev


def load_existing_member_keys(conn: sqlite3.Connection) -> dict[tuple[str, str, str], int]:
    rows = conn.execute("SELECT id, nom, prenom, telephone FROM membres").fetchall()
    out: dict[tuple[str, str, str], int] = {}
    for r in rows:
        k = excel_member_key(r["nom"], r["prenom"], r["telephone"])
        out[k] = int(r["id"])
    return out


def contribution_note_import(sheet_label: str, month_fr: str) -> str:
    return f"Excel {sheet_label} {month_fr.capitalize()}"


def contribution_exists(
    conn: sqlite3.Connection,
    membre_id: int,
    date_iso: str,
    montant: float,
    note: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM contributions
        WHERE membre_id = ? AND date = ? AND ABS(montant - ?) < 0.001 AND note = ?
        LIMIT 1
        """,
        (membre_id, date_iso, montant, note),
    ).fetchone()
    return row is not None


def depense_exists(conn: sqlite3.Connection, description: str, date_iso: str, montant: float) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM depenses
        WHERE description = ? AND date = ? AND ABS(montant - ?) < 0.001
        LIMIT 1
        """,
        (description, date_iso, montant),
    ).fetchone()
    return row is not None


def insert_membre_from_import(
    conn: sqlite3.Connection,
    nom: str,
    prenom: str,
    telephone: str,
    village_origine: str,
    prefecture: str,
    email: str,
    adresse: str,
    date_inscription: date,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO membres(reference, nom, prenom, telephone, village_origine, adresse, prefecture, email, date_inscription, actif)
        VALUES('', ?, ?, ?, ?, ?, ?, ?, ?, 1);
        """,
        (
            nom.strip(),
            prenom.strip(),
            telephone.strip(),
            village_origine.strip(),
            adresse.strip(),
            prefecture.strip(),
            email.strip(),
            to_iso(date_inscription),
        ),
    )
    new_id = int(cur.lastrowid)
    conn.execute("UPDATE membres SET reference = ? WHERE id = ?", (member_ref(new_id), new_id))
    log_activity(
        conn,
        type_operation="CREATE",
        entite="membre",
        entite_id=new_id,
        details=(
            f"Import Excel ref={member_ref(new_id)} nom={nom.strip()} prenom={prenom.strip()} "
            f"village={village_origine or '-'} tel={telephone or '-'}"
        ),
    )
    return new_id


def parse_workbook_preview(
    raw: bytes,
    cotisation_sheets: list[str],
    depense_sheets: list[str],
    import_reports: bool,
    default_inscription: date,
) -> dict[str, Any]:
    xl = pd.ExcelFile(BytesIO(raw), engine="openpyxl")

    members: dict[MemberImportUid, dict[str, str]] = {}
    contributions: list[dict[str, object]] = []
    reports: list[dict[str, object]] = []
    depenses: list[dict[str, object]] = []

    month_labels = [
        "janvier",
        "fevrier",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "aout",
        "septembre",
        "octobre",
        "novembre",
        "decembre",
    ]

    for sheet in cotisation_sheets:
        sheet_year = cotisation_sheet_year(sheet)
        if sheet_year is None:
            continue
        df = pd.read_excel(BytesIO(raw), sheet_name=sheet, header=3, engine="openpyxl")
        col_nom = resolve_col(df, "Nom")
        col_prenom = resolve_col(df, "Prenom", "Prénom")
        col_tel = resolve_col(df, "telephone", "téléphone", "tel")
        col_pref = resolve_col(df, "Prefecture", "Préfecture", "prefecture")
        col_mail = resolve_col(df, "email", "e-mail")
        col_addr = resolve_col(df, "adresse")
        if not col_nom or not col_prenom:
            continue

        month_cols = month_columns_from_df(df)
        solde_cols = solde_columns_from_df(df) if import_reports else []

        for row_num, (_, row) in enumerate(df.iterrows(), start=1):
            nom_v = row[col_nom]
            prenom_v = row[col_prenom]
            if pd.isna(nom_v) or str(nom_v).strip() == "":
                continue
            if pd.isna(prenom_v) or str(prenom_v).strip() == "":
                continue

            uid = member_import_uid(nom_v, prenom_v, row[col_tel] if col_tel else "", sheet, row_num)
            tel_str = normalize_phone(row[col_tel]) if col_tel else ""
            pref = str(row[col_pref]).strip() if col_pref and pd.notna(row[col_pref]) else ""
            mail = str(row[col_mail]).strip() if col_mail and pd.notna(row[col_mail]) else ""
            addr = str(row[col_addr]).strip() if col_addr and pd.notna(row[col_addr]) else ""

            if uid not in members:
                members[uid] = {
                    "nom": str(nom_v).strip(),
                    "prenom": str(prenom_v).strip(),
                    "telephone": tel_str,
                    "village_origine": pref,
                    "prefecture": pref,
                    "email": mail,
                    "adresse": addr,
                }
            else:
                m = members[uid]
                m["telephone"] = merge_member_field(m["telephone"], tel_str if tel_str else None)
                m["email"] = merge_member_field(m["email"], mail if mail else None)
                m["adresse"] = merge_member_field(m["adresse"], addr if addr else None)
                m["village_origine"] = merge_member_field(m["village_origine"], pref if pref else None)
                m["prefecture"] = merge_member_field(m["prefecture"], pref if pref else None)

            for mi, col_name in month_cols.items():
                amt = parse_money_cell(row[col_name])
                if amt is None:
                    continue
                d = last_day_of_month(sheet_year, mi)
                contributions.append(
                    {
                        "member_uid": uid,
                        "nom": str(nom_v).strip(),
                        "prenom": str(prenom_v).strip(),
                        "telephone": tel_str,
                        "annee": sheet_year,
                        "mois": mi,
                        "montant": round(float(amt), 2),
                        "date_iso": d.isoformat(),
                        "note": contribution_note_import(sheet, month_labels[mi - 1]),
                    }
                )

            if import_reports:
                for solde_year, col_name in solde_cols:
                    report_annee = solde_year + 1
                    rep_amt = parse_solde_report_amount(row[col_name])
                    if rep_amt is None:
                        continue
                    if rep_amt <= 0:
                        continue
                    reports.append(
                        {
                            "member_uid": uid,
                            "nom": str(nom_v).strip(),
                            "prenom": str(prenom_v).strip(),
                            "telephone": tel_str,
                            "report_annee": report_annee,
                            "montant_du": float(rep_amt),
                            "source_col": col_name,
                            "source_sheet": sheet,
                        }
                    )

    for sheet in depense_sheets:
        year_d = depenses_sheet_year(sheet)
        if year_d is None:
            continue
        df = pd.read_excel(BytesIO(raw), sheet_name=sheet, header=2, engine="openpyxl")
        col_lib = resolve_col(df, "Libellé", "Libelle")
        if not col_lib:
            continue
        month_cols = month_columns_from_df(df)

        for _, row in df.iterrows():
            lib_v = row[col_lib]
            if pd.isna(lib_v) or str(lib_v).strip() == "":
                continue
            desc = str(lib_v).strip()
            for mi, col_name in month_cols.items():
                amt = parse_money_cell(row[col_name])
                if amt is None:
                    continue
                d = last_day_of_month(year_d, mi)
                depenses.append(
                    {
                        "description": desc[:500],
                        "montant": round(float(amt), 2),
                        "date_iso": d.isoformat(),
                        "source_sheet": sheet,
                    }
                )

    return {
        "members": members,
        "contributions": contributions,
        "reports": reports,
        "depenses": depenses,
        "default_inscription": default_inscription,
        "xl_sheet_names": xl.sheet_names,
    }


def apply_import_bundle(conn: sqlite3.Connection, bundle: dict[str, Any]) -> dict[str, int]:
    """Écrit en base après validation. Retourne des compteurs."""
    members: dict[MemberImportUid, dict[str, str]] = bundle["members"]
    contributions: list[dict[str, object]] = bundle["contributions"]
    reports: list[dict[str, object]] = bundle["reports"]
    depenses_rows: list[dict[str, object]] = bundle["depenses"]
    default_inscription: date = bundle["default_inscription"]

    db_key_to_id = load_existing_member_keys(conn)
    uid_to_id: dict[MemberImportUid, int] = {}
    created = 0
    for uid, info in members.items():
        dk = excel_member_key(info["nom"], info["prenom"], info["telephone"])
        if dk in db_key_to_id:
            uid_to_id[uid] = db_key_to_id[dk]
            continue
        mid = insert_membre_from_import(
            conn,
            info["nom"],
            info["prenom"],
            info["telephone"],
            info["village_origine"],
            info["prefecture"],
            info["email"],
            info["adresse"],
            default_inscription,
        )
        db_key_to_id[dk] = mid
        uid_to_id[uid] = mid
        created += 1

    contrib_ins = 0
    contrib_skip = 0
    for c in contributions:
        uid = c["member_uid"]
        if uid not in uid_to_id:
            contrib_skip += 1
            continue
        mid = uid_to_id[uid]
        note = str(c["note"])
        montant = float(c["montant"])
        date_iso = str(c["date_iso"])
        if contribution_exists(conn, mid, date_iso, montant, note):
            contrib_skip += 1
            continue
        conn.execute(
            "INSERT INTO contributions(membre_id, montant, date, note) VALUES(?, ?, ?, ?)",
            (mid, montant, date_iso, note),
        )
        contrib_ins += 1

    dep_ins = 0
    dep_skip = 0
    for d in depenses_rows:
        desc = str(d["description"])
        montant = float(d["montant"])
        date_iso = str(d["date_iso"])
        if depense_exists(conn, desc, date_iso, montant):
            dep_skip += 1
            continue
        conn.execute(
            "INSERT INTO depenses(description, montant, date) VALUES(?, ?, ?)",
            (desc, montant, date_iso),
        )
        dep_ins += 1

    rep_ins = 0
    for r in reports:
        uid = r["member_uid"]
        if uid not in uid_to_id:
            continue
        mid = uid_to_id[uid]
        annee = int(r["report_annee"])
        amt = float(r["montant_du"])
        conn.execute(
            """
            INSERT INTO reports_membres(membre_id, annee, montant_du)
            VALUES(?, ?, ?)
            ON CONFLICT(membre_id, annee) DO UPDATE SET montant_du = excluded.montant_du;
            """,
            (mid, annee, amt),
        )
        rep_ins += 1

    log_activity(
        conn,
        type_operation="CREATE",
        entite="import_excel",
        entite_id=None,
        details=(
            f"Import Excel applique: membres_crees={created}, contributions={contrib_ins} "
            f"(ignores {contrib_skip}), depenses={dep_ins} (ignores {dep_skip}), reports={rep_ins}"
        ),
    )
    conn.commit()
    return {
        "membres_crees": created,
        "contributions": contrib_ins,
        "contributions_ignorees": contrib_skip,
        "depenses": dep_ins,
        "depenses_ignorees": dep_skip,
        "reports": rep_ins,
    }


def ensure_openpyxl_or_stop() -> None:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        st.error(
            "**openpyxl** est nécessaire pour lire les fichiers Excel (.xlsx).\n\n"
            "- **En local** : exécutez `python -m pip install openpyxl` "
            "(ou `python -m pip install -r requirements.txt` à la racine du projet).\n\n"
            "- **Sur Streamlit Community Cloud** : le fichier `requirements.txt` doit être "
            "à la **racine du dépôt Git** lié à l’app (avec une ligne `openpyxl>=3.1.0`), "
            "puis redémarrez l’app (**Manage app → Reboot**) ou refaites un déploiement."
        )
        st.stop()


def page_import_excel(conn: sqlite3.Connection) -> None:
    st.subheader("Import Excel")
    ensure_openpyxl_or_stop()
    st.caption(
        "Feuilles attendues : « Cotisations AAAA » (en-tête membres ligne 4) et « Dépenses AAAA » "
        "(en-tête ligne 3). Aperçu obligatoire avant écriture dans la base."
    )

    up = st.file_uploader("Fichier Excel (.xlsx)", type=["xlsx"])
    default_date_ins = st.date_input(
        "Date d'inscription pour les nouveaux membres créés par l'import",
        value=date(date.today().year, 1, 1),
        key="import_default_inscription",
    )
    import_reports = st.checkbox(
        "Importer les colonnes « Solde YYYY » comme report membre (montant dû pour l'année YYYY+1 ; "
        "valeurs négatives Excel = retard converti en montant positif)",
        value=False,
    )
    st.session_state.setdefault("_import_bundle", None)

    if not up:
        st.info("Chargez le classeur AGPM (ex. AGPM Association_2026.xlsx).")
        return

    raw = up.getvalue()
    try:
        xl_names = pd.ExcelFile(BytesIO(raw), engine="openpyxl").sheet_names
    except Exception as e:
        st.error(f"Lecture du fichier impossible : {e}")
        return

    cotisation_candidates = [n for n in xl_names if cotisation_sheet_year(n) is not None]
    depense_candidates = [n for n in xl_names if depenses_sheet_year(n) is not None]

    c_sel = st.multiselect(
        "Feuilles cotisations à importer",
        options=sorted(cotisation_candidates),
        default=sorted(cotisation_candidates),
    )
    d_sel = st.multiselect(
        "Feuilles dépenses à importer",
        options=sorted(depense_candidates),
        default=sorted(depense_candidates),
    )

    if st.button("Préparer l'aperçu", type="primary"):
        try:
            bundle = parse_workbook_preview(
                raw,
                list(c_sel),
                list(d_sel),
                import_reports,
                default_date_ins,
            )
            st.session_state["_import_bundle"] = bundle
        except Exception as e:
            st.session_state["_import_bundle"] = None
            st.exception(e)

    bundle = st.session_state.get("_import_bundle")
    if not bundle:
        return

    existing = load_existing_member_keys(conn)
    members_dict: dict[MemberImportUid, dict[str, str]] = bundle["members"]
    rows_mem = []
    for uid, info in sorted(members_dict.items(), key=lambda x: (x[1]["nom"], x[1]["prenom"], str(x[0]))):
        dk = excel_member_key(info["nom"], info["prenom"], info["telephone"])
        rows_mem.append(
            {
                "_uid_json": uid_import_to_json(uid),
                "origine_ligne": member_import_row_label(uid),
                "nouveau": "Oui" if dk not in existing else "Non",
                "id_existant": str(existing[dk]) if dk in existing else "",
                "nom": info["nom"],
                "prenom": info["prenom"],
                "telephone": info["telephone"] if info["telephone"] else "",
                "email": info["email"],
                "prefecture": info["prefecture"],
            }
        )
    df_mem = pd.DataFrame(rows_mem)

    st.markdown("### Contacts à importer — complétez les téléphones si besoin")
    st.caption(
        "Sans numéro dans Excel, chaque ligne de cotisation reste une entrée distincte (homonymes non fusionnés). "
        "Renseignez le téléphone ci-dessous puis fusionnez les doublons, avant d'écrire en base."
    )
    edited_mem = st.data_editor(
        df_mem,
        column_config={
            "_uid_json": st.column_config.TextColumn("UID", disabled=True, width="small"),
            "origine_ligne": st.column_config.TextColumn("Source Excel", disabled=True),
            "nouveau": st.column_config.TextColumn("Nouveau ?", disabled=True),
            "id_existant": st.column_config.TextColumn("Id base", disabled=True),
            "nom": st.column_config.TextColumn("Nom", disabled=True),
            "prenom": st.column_config.TextColumn("Prénom", disabled=True),
            "telephone": st.column_config.TextColumn("Téléphone", required=True),
            "email": st.column_config.TextColumn("Email", disabled=True),
            "prefecture": st.column_config.TextColumn("Préfecture", disabled=True),
        },
        hide_index=True,
        num_rows="fixed",
        use_container_width=True,
        key="import_editor_membres",
    )

    if st.button("Appliquer téléphones et fusionner les doublons", type="secondary"):
        sync_member_phones_from_editor(bundle, edited_mem)
        collapse_bundle_members_by_phone(bundle)
        st.session_state["_import_bundle"] = bundle
        st.rerun()
    st.caption(
        "Après saisie des numéros : utilisez ce bouton pour regrouper les fiches identiques "
        "(même nom, prénom et téléphone). L'enregistrement en base synchronise aussi les champs depuis le tableau."
    )

    cdf = pd.DataFrame(bundle["contributions"])
    if cdf.empty:
        st.warning("Aucune cotisation mensuelle numérique détectée dans les feuilles sélectionnées.")
    else:
        st.markdown(f"### Aperçu — contributions ({len(cdf)} lignes)")
        show_c = cdf.copy()

        def _deja_membre(r: pd.Series) -> bool:
            uid = r["member_uid"]
            info = bundle["members"].get(uid)
            if not info:
                return False
            dk = excel_member_key(info["nom"], info["prenom"], info["telephone"])
            return dk in existing

        show_c["telephone"] = show_c["member_uid"].apply(
            lambda u: bundle["members"].get(u, {}).get("telephone", "")
        )
        show_c["deja_membre"] = show_c.apply(_deja_membre, axis=1)
        show_c = show_c.drop(columns=["member_uid"], errors="ignore")
        st.dataframe(show_c.head(500), use_container_width=True)
        if len(show_c) > 500:
            st.caption(f"Affichage tronqué : {len(show_c) - 500} lignes supplémentaires non affichées.")

    ddf = pd.DataFrame(bundle["depenses"])
    if not ddf.empty:
        st.markdown(f"### Aperçu — dépenses ({len(ddf)} lignes)")
        st.dataframe(ddf.head(300), use_container_width=True)

    rdf = pd.DataFrame(bundle["reports"])
    if import_reports:
        if rdf.empty:
            st.info("Aucune valeur « Solde » retenue (vide ou zéro après conversion).")
        else:
            st.markdown(f"### Aperçu — reports membres ({len(rdf)} lignes)")
            st.dataframe(rdf.drop(columns=["member_uid"], errors="ignore").head(300), use_container_width=True)

    st.markdown("### Validation")
    if st.button("Écrire dans la base SQLite", type="primary"):
        try:
            sync_member_phones_from_editor(bundle, edited_mem)
            collapse_bundle_members_by_phone(bundle)
            manques = validate_bundle_phones(bundle)
            if manques:
                st.error(
                    "Téléphone manquant pour les contacts suivants — complétez la colonne puis "
                    "« Appliquer téléphones… » ou réessayez :\n\n"
                    + "\n".join(f"- {m}" for m in manques[:40])
                    + (f"\n… ({len(manques) - 40} autres)" if len(manques) > 40 else "")
                )
                st.session_state["_import_bundle"] = bundle
            else:
                counts = apply_import_bundle(conn, bundle)
                st.success(
                    f"Import terminé — membres créés : {counts['membres_crees']}, "
                    f"cotisations : {counts['contributions']} (ignorées doublons : {counts['contributions_ignorees']}), "
                    f"dépenses : {counts['depenses']} (ignorées : {counts['depenses_ignorees']}), "
                    f"reports : {counts['reports']}."
                )
                st.session_state["_import_bundle"] = None
        except Exception as e:
            st.exception(e)


def main() -> None:
    st.set_page_config(page_title="AGPM - Gestion Association", layout="wide")
    st.title("AGPM - Gestion de l'association")
    st.caption("Cotisation mensuelle de référence: 10 EUR.")

    conn = get_conn()
    init_db(conn)

    menu = st.sidebar.radio(
        "Navigation",
        ["Membres", "Contributions", "Dépenses", "Dashboard", "Activité", "Import Excel"],
    )
    st.sidebar.info("Les calculs sont basés sur les transactions réelles, pas sur des cellules Excel.")

    if menu == "Membres":
        page_membres(conn)
    elif menu == "Contributions":
        page_contributions(conn)
    elif menu == "Dépenses":
        page_depenses(conn)
    elif menu == "Activité":
        page_activite(conn)
    elif menu == "Import Excel":
        page_import_excel(conn)
    else:
        page_dashboard(conn)


if __name__ == "__main__":
    main()
