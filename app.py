import sqlite3
from datetime import date, datetime
from typing import Optional

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
            "SELECT id, actif, reference, nom, prenom FROM membres WHERE id = ?",
            (selected_id,),
        ).fetchone()
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


def main() -> None:
    st.set_page_config(page_title="AGPM - Gestion Association", layout="wide")
    st.title("AGPM - Gestion de l'association")
    st.caption("Cotisation mensuelle de référence: 10 EUR.")

    conn = get_conn()
    init_db(conn)

    menu = st.sidebar.radio("Navigation", ["Membres", "Contributions", "Dépenses", "Dashboard", "Activité"])
    st.sidebar.info("Les calculs sont basés sur les transactions réelles, pas sur des cellules Excel.")

    if menu == "Membres":
        page_membres(conn)
    elif menu == "Contributions":
        page_contributions(conn)
    elif menu == "Dépenses":
        page_depenses(conn)
    elif menu == "Activité":
        page_activite(conn)
    else:
        page_dashboard(conn)


if __name__ == "__main__":
    main()
