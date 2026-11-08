import os
import sqlite3
import uuid
from functools import wraps
from datetime import datetime

from flask import (
    Flask, render_template, render_template_string, request, redirect,
    url_for, flash, session, abort, send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "campeonatos.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "troque-esta-chave-antes-de-publicar-" + str(uuid.uuid4())
)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
ALLOWED = {"png", "jpg", "jpeg", "webp", "pdf"}


class PostgresCompat:
    def __init__(self, con):
        self.con = con

    def execute(self, sql, params=()):
        return self.con.execute(sql.replace("?", "%s"), params)

    def executescript(self, script):
        for command in script.split(";"):
            if command.strip():
                self.con.execute(command)

    def commit(self):
        self.con.commit()

    def close(self):
        self.con.close()


def db():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        import psycopg
        from psycopg.rows import dict_row

        database_url = database_url.replace("postgres://", "postgresql://", 1)
        con = psycopg.connect(database_url, row_factory=dict_row)
        return PostgresCompat(con)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    con = db()
    postgres = bool(os.environ.get("DATABASE_URL"))
    pk = "SERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"

    con.executescript(f"""
    CREATE TABLE IF NOT EXISTS users(
      id {pk}, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
      password TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('admin','organizador','tecnico')),
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS championships(
      id {pk}, name TEXT NOT NULL, city TEXT NOT NULL, state TEXT NOT NULL,
      year INTEGER NOT NULL, organizer_id INTEGER NOT NULL,
      description TEXT DEFAULT '', prize TEXT DEFAULT '',
      status TEXT DEFAULT 'Inscrições abertas',
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(organizer_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS teams(
      id {pk}, championship_id INTEGER NOT NULL, name TEXT NOT NULL,
      city TEXT DEFAULT '', coach_id INTEGER NOT NULL, shield TEXT DEFAULT '',
      status TEXT DEFAULT 'Pendente',
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(championship_id) REFERENCES championships(id) ON DELETE CASCADE,
      FOREIGN KEY(coach_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS players(
      id {pk}, team_id INTEGER NOT NULL, name TEXT NOT NULL,
      birth_date TEXT DEFAULT '', position TEXT DEFAULT '',
      shirt_number INTEGER, photo TEXT DEFAULT '', document TEXT DEFAULT '',
      FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS matches(
      id {pk}, championship_id INTEGER NOT NULL,
      round_name TEXT DEFAULT '', match_date TEXT DEFAULT '',
      venue TEXT DEFAULT '', home_team INTEGER NOT NULL,
      away_team INTEGER NOT NULL, home_goals INTEGER,
      away_goals INTEGER, status TEXT DEFAULT 'Agendado',
      FOREIGN KEY(championship_id) REFERENCES championships(id) ON DELETE CASCADE,
      FOREIGN KEY(home_team) REFERENCES teams(id),
      FOREIGN KEY(away_team) REFERENCES teams(id)
    );

    CREATE TABLE IF NOT EXISTS goals(
      id {pk}, match_id INTEGER NOT NULL, player_id INTEGER NOT NULL,
      quantity INTEGER NOT NULL DEFAULT 1,
      FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
      FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS cards(
      id {pk}, match_id INTEGER NOT NULL, player_id INTEGER NOT NULL,
      card_type TEXT NOT NULL CHECK(card_type IN ('Amarelo','Vermelho')),
      FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
      FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
    );
    """)

    admin = con.execute(
        "SELECT id FROM users WHERE role='admin' LIMIT 1"
    ).fetchone()

    if not admin:
        con.execute(
            "INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)",
            (
                "Administrador",
                "admin@varzea.local",
                generate_password_hash("Admin@12345"),
                "admin"
            )
        )

    con.commit()
    con.close()


def current_user():
    if not session.get("user_id"):
        return None
    con = db()
    user = con.execute(
        "SELECT id,name,email,role FROM users WHERE id=?",
        (session["user_id"],)
    ).fetchone()
    con.close()
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("Entre na sua conta para continuar.", "warning")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def roles(*allowed):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if user["role"] not in allowed:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return deco


def save_upload(field):
    file = request.files.get(field)
    if not file or not file.filename:
        return ""
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED:
        raise ValueError("Formato não permitido. Use JPG, PNG, WEBP ou PDF.")
    filename = uuid.uuid4().hex + "_" + secure_filename(file.filename)
    file.save(os.path.join(UPLOAD_DIR, filename))
    return filename


@app.context_processor
def inject_user():
    return {"user": current_user()}


@app.route("/")
def index():
    con = db()
    championships = con.execute("""
        SELECT c.*, u.name organizer,
        (SELECT COUNT(*) FROM teams t
         WHERE t.championship_id=c.id AND t.status='Aprovada') team_count
        FROM championships c
        JOIN users u ON u.id=c.organizer_id
        ORDER BY c.id DESC
    """).fetchall()
    con.close()
    return render_template("index.html", championships=championships)


@app.route("/cadastro", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "tecnico")

        if role not in ("tecnico", "organizador"):
            role = "tecnico"

        if not name or not email or len(password) < 8:
            flash(
                "Preencha os campos. A senha precisa ter pelo menos 8 caracteres.",
                "danger"
            )
        else:
            con = db()
            try:
                con.execute(
                    "INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)",
                    (name, email, generate_password_hash(password), role)
                )
                con.commit()
                flash("Conta criada! Faça login.", "success")
                return redirect(url_for("login"))
            except Exception as error:
                if "unique" in str(error).lower() or "duplicate" in str(error).lower():
                    flash("Este e-mail já está cadastrado.", "danger")
                else:
                    raise
            finally:
                con.close()

    return render_template("register.html")


@app.route("/campeonato/<int:cid>")
def championship(cid):
    con = db()
    championship_data = con.execute("""
        SELECT c.*, u.name organizer
        FROM championships c
        JOIN users u ON u.id=c.organizer_id
        WHERE c.id=?
    """, (cid,)).fetchone()

    if not championship_data:
        con.close()
        abort(404)

    teams = con.execute("""
        SELECT * FROM teams
        WHERE championship_id=? AND status='Aprovada'
        ORDER BY name
    """, (cid,)).fetchall()

    matches = con.execute("""
        SELECT m.*, h.name home_name, a.name away_name
        FROM matches m
        JOIN teams h ON h.id=m.home_team
        JOIN teams a ON a.id=m.away_team
        WHERE m.championship_id=?
        ORDER BY m.match_date,m.id
    """, (cid,)).fetchall()

    standings = build_standings(con, cid)

    scorers = con.execute("""
        SELECT p.name player, t.name team, SUM(g.quantity) goals
        FROM goals g
        JOIN players p ON p.id=g.player_id
        JOIN teams t ON t.id=p.team_id
        JOIN matches m ON m.id=g.match_id
        WHERE m.championship_id=?
        GROUP BY p.id, p.name, t.name
        ORDER BY goals DESC,p.name
        LIMIT 30
    """, (cid,)).fetchall()

    players = con.execute("""
        SELECT p.id,p.name,p.team_id,p.shirt_number
        FROM players p
        JOIN teams t ON t.id=p.team_id
        WHERE t.championship_id=? AND t.status='Aprovada'
        ORDER BY t.name,p.name
    """, (cid,)).fetchall()

    players_by_team = {}
    for player in players:
        players_by_team.setdefault(player["team_id"], []).append(player)

    cards = con.execute("""
        SELECT ca.card_type, p.name AS player, t.name AS team,
               m.match_date, m.round_name,
               ht.name AS home_name, at.name AS away_name
        FROM cards ca
        JOIN players p ON p.id=ca.player_id
        JOIN teams t ON t.id=p.team_id
        JOIN matches m ON m.id=ca.match_id
        JOIN teams ht ON ht.id=m.home_team
        JOIN teams at ON at.id=m.away_team
        WHERE m.championship_id=?
        ORDER BY m.match_date DESC, p.name
    """, (cid,)).fetchall()

    con.close()
    return render_template(
        "championship.html",
        c=championship_data,
        teams=teams,
        matches=matches,
        standings=standings,
        scorers=scorers,
        players_by_team=players_by_team,
        cards=cards
    )


def build_standings(con, cid):
    teams = con.execute("""
        SELECT id,name FROM teams
        WHERE championship_id=? AND status='Aprovada'
        ORDER BY name
    """, (cid,)).fetchall()

    stats = {}
    for team in teams:
        stats[team["id"]] = {
            "team": team["name"], "P": 0, "J": 0, "V": 0,
            "E": 0, "D": 0, "GP": 0, "GC": 0, "SG": 0
        }

    games = con.execute("""
        SELECT * FROM matches
        WHERE championship_id=? AND status='Encerrado'
        AND home_goals IS NOT NULL AND away_goals IS NOT NULL
    """, (cid,)).fetchall()

    for match in games:
        home, away = match["home_team"], match["away_team"]
        if home not in stats or away not in stats:
            continue

        home_stats, away_stats = stats[home], stats[away]
        hg, ag = match["home_goals"], match["away_goals"]

        home_stats["J"] += 1
        away_stats["J"] += 1
        home_stats["GP"] += hg
        home_stats["GC"] += ag
        away_stats["GP"] += ag
        away_stats["GC"] += hg

        if hg > ag:
            home_stats["V"] += 1
            home_stats["P"] += 3
            away_stats["D"] += 1
        elif hg < ag:
            away_stats["V"] += 1
            away_stats["P"] += 3
            home_stats["D"] += 1
        else:
            home_stats["E"] += 1
            away_stats["E"] += 1
            home_stats["P"] += 1
            away_stats["P"] += 1

    for item in stats.values():
        item["SG"] = item["GP"] - item["GC"]

    return sorted(
        stats.values(),
        key=lambda item: (
            -item["P"], -item["V"], -item["SG"], -item["GP"], item["team"]
        )
    )


@app.route("/entrar", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        con = db()
        user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        con.close()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            flash("Bem-vindo(a)!", "success")
            return redirect(url_for("dashboard"))

        flash("E-mail ou senha incorretos.", "danger")

    return render_template("login.html")


@app.route("/sair")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/painel")
@login_required
def dashboard():
    user = current_user()
    con = db()

    if user["role"] == "admin":
        championships = con.execute("""
            SELECT c.*,u.name organizer
            FROM championships c
            JOIN users u ON u.id=c.organizer_id
            ORDER BY c.id DESC
        """).fetchall()

        pending_teams = con.execute("""
            SELECT t.id,t.name,t.city,t.status,c.name championship,
                   u.name coach_name
            FROM teams t
            JOIN championships c ON c.id=t.championship_id
            JOIN users u ON u.id=t.coach_id
            WHERE t.status='Pendente'
            ORDER BY t.id
        """).fetchall()

        pending = len(pending_teams)
        con.close()

        return render_template_string("""
        <!doctype html>
        <html lang="pt-BR"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Painel do administrador - Campeonatos de Várzea</title>
        <style>
        body{font-family:Arial,sans-serif;background:#f3f6f3;margin:0;color:#203329}
        header{background:#103d2b;color:white;padding:16px 5%;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
        header a{color:white;margin-left:12px}
        main{max-width:1050px;margin:28px auto;padding:0 16px}
        .card{background:white;border:1px solid #dce7df;border-radius:14px;padding:18px;margin:14px 0;box-shadow:0 3px 12px #173b2410}
        .team{display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap}
        .actions{display:flex;gap:8px;flex-wrap:wrap}
        button{border:0;border-radius:8px;padding:10px 15px;font-weight:bold;cursor:pointer}
        .approve{background:#20734d;color:white}.reject{background:#f3dddd;color:#8c2020}
        .view{display:inline-block;background:#e7f1e9;color:#174a32;padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:bold}
        .muted{color:#64756b}.empty{padding:18px;background:#edf5ef;border-radius:10px}
        </style></head><body>
        <header><strong>🏆 CAMPEONATOS DE VÁRZEA</strong>
        <nav><a href="{{ url_for('index') }}">Início</a>
        <a href="{{ url_for('dashboard') }}">Meu painel</a>
        <a href="{{ url_for('logout') }}">Sair (Administrador)</a></nav></header>
        <main><h1>Olá, Administrador!</h1>
        <p class="muted">Inscrições pendentes: <strong>{{ pending }}</strong></p>
        <section class="card"><h2>Inscrições para aprovação</h2>
        {% if pending_teams %}
          {% for t in pending_teams %}
          <div class="card team">
            <div><h3>{{ t.name }}</h3>
              <p class="muted">Campeonato: {{ t.championship }} · Cidade: {{ t.city or 'Não informada' }}</p>
              <p class="muted">Técnico responsável: {{ t.coach_name }}</p>
              <a class="view" href="{{ url_for('team_detail', tid=t.id) }}">Ver equipe e jogadores</a>
            </div>
            <div class="actions">
              <form method="post" action="{{ url_for('team_status', tid=t.id) }}">
                <input type="hidden" name="status" value="Aprovada">
                <button class="approve" type="submit">Aprovar equipe</button>
              </form>
              <form method="post" action="{{ url_for('team_status', tid=t.id) }}">
                <input type="hidden" name="status" value="Rejeitada">
                <button class="reject" type="submit">Rejeitar</button>
              </form>
            </div>
          </div>
          {% endfor %}
        {% else %}
          <div class="empty">Não há inscrições pendentes no momento.</div>
        {% endif %}</section>
        <h2>Seus campeonatos</h2>
        {% for c in championships %}
          <div class="card"><h3>{{ c.name }}</h3>
          <p class="muted">{{ c.city }} - {{ c.state }} · {{ c.year }}</p>
          <a class="view" href="{{ url_for('championship', cid=c.id) }}">Ver página pública</a></div>
        {% else %}
          <p>Nenhum campeonato cadastrado.</p>
        {% endfor %}
        </main></body></html>
        """, championships=championships, pending=pending, pending_teams=pending_teams)

    if user["role"] == "organizador":
        championships = con.execute("""
            SELECT * FROM championships
            WHERE organizer_id=? ORDER BY id DESC
        """, (user["id"],)).fetchall()
        pending = con.execute("""
            SELECT COUNT(*) n FROM teams t
            JOIN championships c ON c.id=t.championship_id
            WHERE c.organizer_id=? AND t.status='Pendente'
        """, (user["id"],)).fetchone()["n"]
    else:
        championships = con.execute("""
            SELECT c.*,t.id team_id,t.name team_name,t.status team_status
            FROM teams t
            JOIN championships c ON c.id=t.championship_id
            WHERE t.coach_id=? ORDER BY t.id DESC
        """, (user["id"],)).fetchall()
        pending = 0

    con.close()
    return render_template(
        "dashboard.html", championships=championships, pending=pending
    )


@app.route("/campeonato/novo", methods=["GET", "POST"])
@roles("admin", "organizador")
def new_championship():
    if request.method == "POST":
        form = request.form
        if not form.get("name") or not form.get("city") or not form.get("state"):
            flash("Informe nome, cidade e estado.", "danger")
        else:
            con = db()
            con.execute("""
                INSERT INTO championships(
                    name,city,state,year,organizer_id,description,prize,status
                ) VALUES(?,?,?,?,?,?,?,?)
            """, (
                form["name"].strip(),
                form["city"].strip(),
                form["state"].upper()[:2],
                int(form.get("year") or datetime.now().year),
                current_user()["id"],
                form.get("description", ""),
                form.get("prize", ""),
                form.get("status", "Inscrições abertas")
            ))
            con.commit()
            con.close()
            flash("Campeonato cadastrado!", "success")
            return redirect(url_for("dashboard"))

    return render_template("new_championship.html")


@app.route("/campeonato/<int:cid>/inscrever", methods=["GET", "POST"])
@roles("tecnico")
def enroll_team(cid):
    con = db()
    championship_data = con.execute(
        "SELECT * FROM championships WHERE id=?", (cid,)
    ).fetchone()

    if not championship_data:
        con.close()
        abort(404)

    if championship_data["status"] != "Inscrições abertas":
        con.close()
        flash("As inscrições não estão abertas.", "warning")
        return redirect(url_for("championship", cid=cid))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        city = request.form.get("city", "").strip()

        try:
            shield = save_upload("shield")
        except ValueError as error:
            con.close()
            flash(str(error), "danger")
            return redirect(url_for("enroll_team", cid=cid))

        if not name:
            con.close()
            flash("Informe o nome da equipe.", "danger")
            return redirect(url_for("enroll_team", cid=cid))

        if os.environ.get("DATABASE_URL"):
            cursor = con.execute("""
                INSERT INTO teams(
                    championship_id,name,city,coach_id,shield
                ) VALUES(?,?,?,?,?) RETURNING id
            """, (cid, name, city, current_user()["id"], shield))
            team_id = cursor.fetchone()["id"]
        else:
            cursor = con.execute("""
                INSERT INTO teams(
                    championship_id,name,city,coach_id,shield
                ) VALUES(?,?,?,?,?)
            """, (cid, name, city, current_user()["id"], shield))
            team_id = cursor.lastrowid

        names = request.form.getlist("player_name[]")
        births = request.form.getlist("birth_date[]")
        positions = request.form.getlist("position[]")
        numbers = request.form.getlist("shirt_number[]")

        for i, player_name in enumerate(names):
            if not player_name.strip():
                continue
            try:
                photo = save_upload(f"photo_{i}")
                document = save_upload(f"document_{i}")
            except ValueError as error:
                con.close()
                flash(str(error), "danger")
                return redirect(url_for("enroll_team", cid=cid))

            number = (
                numbers[i]
                if i < len(numbers) and numbers[i].isdigit()
                else None
            )
            con.execute("""
                INSERT INTO players(
                    team_id,name,birth_date,position,shirt_number,photo,document
                ) VALUES(?,?,?,?,?,?,?)
            """, (
                team_id,
                player_name.strip(),
                births[i] if i < len(births) else "",
                positions[i] if i < len(positions) else "",
                number,
                photo,
                document
            ))

        con.commit()
        con.close()
        flash("Inscrição enviada para análise do organizador.", "success")
        return redirect(url_for("dashboard"))

    con.close()
    return render_template("enroll.html", c=championship_data)


@app.route("/equipe/<int:tid>")
@login_required
def team_detail(tid):
    con = db()
    team = con.execute("""
        SELECT t.*,c.name championship,c.organizer_id
        FROM teams t
        JOIN championships c ON c.id=t.championship_id
        WHERE t.id=?
    """, (tid,)).fetchone()

    if not team:
        con.close()
        abort(404)

    user = current_user()
    if (
        user["role"] != "admin"
        and not (
            user["role"] == "organizador"
            and team["organizer_id"] == user["id"]
        )
        and not (
            user["role"] == "tecnico"
            and team["coach_id"] == user["id"]
        )
    ):
        con.close()
        abort(403)

    players = con.execute(
        "SELECT * FROM players WHERE team_id=? ORDER BY name", (tid,)
    ).fetchall()
    con.close()
    return render_template("team_detail.html", t=team, players=players)


@app.route("/equipe/<int:tid>/status", methods=["POST"])
@roles("admin", "organizador")
def team_status(tid):
    con = db()
    team = con.execute("""
        SELECT t.*,c.organizer_id
        FROM teams t
        JOIN championships c ON c.id=t.championship_id
        WHERE t.id=?
    """, (tid,)).fetchone()

    if not team:
        con.close()
        abort(404)

    user = current_user()
    if user["role"] == "organizador" and team["organizer_id"] != user["id"]:
        con.close()
        abort(403)

    status = request.form.get("status")
    if status not in ("Aprovada", "Rejeitada", "Pendente"):
        con.close()
        abort(400)

    con.execute("UPDATE teams SET status=? WHERE id=?", (status, tid))
    con.commit()
    con.close()
    flash("Situação da equipe atualizada.", "success")
    return redirect(url_for("team_detail", tid=tid))


@app.route("/campeonato/<int:cid>/jogo/novo", methods=["GET", "POST"])
@roles("admin", "organizador")
def new_match(cid):
    con = db()
    championship_data = con.execute(
        "SELECT * FROM championships WHERE id=?", (cid,)
    ).fetchone()

    if not championship_data:
        con.close()
        abort(404)

    if (
        current_user()["role"] == "organizador"
        and championship_data["organizer_id"] != current_user()["id"]
    ):
        con.close()
        abort(403)

    teams = con.execute("""
        SELECT id,name FROM teams
        WHERE championship_id=? AND status='Aprovada'
        ORDER BY name
    """, (cid,)).fetchall()

    if request.method == "POST":
        try:
            home = int(request.form["home_team"])
            away = int(request.form["away_team"])
            if home == away:
                raise ValueError()
            valid = {team["id"] for team in teams}
            if home not in valid or away not in valid:
                raise ValueError()
        except Exception:
            con.close()
            flash("Escolha duas equipes aprovadas diferentes.", "danger")
            return redirect(url_for("new_match", cid=cid))

        con.execute("""
            INSERT INTO matches(
                championship_id,round_name,match_date,venue,home_team,away_team
            ) VALUES(?,?,?,?,?,?)
        """, (
            cid,
            request.form.get("round_name", ""),
            request.form.get("match_date", ""),
            request.form.get("venue", ""),
            home,
            away
        ))
        con.commit()
        con.close()
        flash("Jogo criado.", "success")
        return redirect(url_for("championship", cid=cid))

    con.close()
    return render_template(
        "new_match.html", c=championship_data, teams=teams
    )


@app.route("/jogo/<int:mid>/resultado", methods=["POST"])
@roles("admin", "organizador")
def match_result(mid):
    con = db()
    match = con.execute("""
        SELECT m.*,c.organizer_id
        FROM matches m
        JOIN championships c ON c.id=m.championship_id
        WHERE m.id=?
    """, (mid,)).fetchone()

    if not match:
        con.close()
        abort(404)

    if (
        current_user()["role"] == "organizador"
        and match["organizer_id"] != current_user()["id"]
    ):
        con.close()
        abort(403)

    try:
        home_goals = int(request.form["home_goals"])
        away_goals = int(request.form["away_goals"])
        if home_goals < 0 or away_goals < 0:
            raise ValueError()
    except Exception:
        con.close()
        flash("Informe placares válidos.", "danger")
        return redirect(url_for("championship", cid=match["championship_id"]))

    con.execute("""
        UPDATE matches
        SET home_goals=?,away_goals=?,status='Encerrado'
        WHERE id=?
    """, (home_goals, away_goals, mid))

    con.execute("DELETE FROM goals WHERE match_id=?", (mid,))
    con.execute("DELETE FROM cards WHERE match_id=?", (mid,))

    player_ids = request.form.getlist("scorer_player[]")
    quantities = request.form.getlist("scorer_qty[]")

    for player_id, quantity in zip(player_ids, quantities):
        try:
            quantity = int(quantity)
            player_id = int(player_id)
        except (TypeError, ValueError):
            continue

        player = con.execute(
            "SELECT id,team_id FROM players WHERE id=?", (player_id,)
        ).fetchone()

        if (
            player
            and player["team_id"] in (match["home_team"], match["away_team"])
            and quantity > 0
        ):
            con.execute("""
                INSERT INTO goals(match_id,player_id,quantity)
                VALUES(?,?,?)
            """, (mid, player_id, quantity))

    card_players = request.form.getlist("card_player[]")
    card_types = request.form.getlist("card_type[]")

    for player_id, card_type in zip(card_players, card_types):
        if not player_id or card_type not in ("Amarelo", "Vermelho"):
            continue
        try:
            player_id = int(player_id)
        except (TypeError, ValueError):
            continue

        player = con.execute(
            "SELECT id,team_id FROM players WHERE id=?", (player_id,)
        ).fetchone()

        if player and player["team_id"] in (
            match["home_team"], match["away_team"]
        ):
            con.execute("""
                INSERT INTO cards(match_id,player_id,card_type)
                VALUES(?,?,?)
            """, (mid, player_id, card_type))

    con.commit()
    con.close()
    flash("Resultado e cartões registrados.", "success")
    return redirect(url_for("championship", cid=match["championship_id"]))


@app.route("/arquivo/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.errorhandler(403)
def forbidden(error):
    return render_template(
        "message.html",
        title="Acesso não permitido",
        message="Sua conta não tem permissão para acessar esta página."
    ), 403


@app.errorhandler(404)
def not_found(error):
    return render_template(
        "message.html",
        title="Página não encontrada",
        message="O conteúdo solicitado não existe."
    ), 404


@app.errorhandler(413)
def too_large(error):
    return render_template(
        "message.html",
        title="Arquivo muito grande",
        message="Cada arquivo deve ter no máximo 12 MB."
    ), 413


@app.route("/service-worker.js")
def service_worker():
    response = app.send_static_file("service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
