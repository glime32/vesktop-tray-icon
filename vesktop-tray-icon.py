#!/usr/bin/env python3
# vesktop-tray-icon : met dans le tray de Vesktop la même icône que celle
# choisie dans le menu KDE (clic droit > Modifier l'application).
#
# Vesktop lit deux fichiers dans ~/.config/vesktop/userAssets/ :
#   tray        -> icône normale
#   trayUnread  -> icône quand il y a des messages non lus
# Ce script les génère (la deuxième avec une pastille rouge) puis relance Vesktop.

import os, sys, re, glob, base64, shutil, signal, shlex, subprocess, tempfile, time

H = os.path.expanduser("~")
CFG = os.environ.get("XDG_CONFIG_HOME") or os.path.join(H, ".config")
DATA = os.environ.get("XDG_DATA_HOME") or os.path.join(H, ".local", "share")
# noms d'icône qui veulent dire "pas modifiée"
DEFAUT = {"", "vesktop", "dev.vencord.vesktop", "vencorddesktop"}


def stop(msg):
    print("✘ " + msg)
    sys.exit(1)


def ini(path, section):
    """Lecture minimale d'un .desktop / kdeglobals (configparser râle sur les clés [$e])."""
    out, cur = {}, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in "#;":
                    continue
                if line[0] == "[" and line[-1] == "]":
                    cur = line[1:-1]
                elif cur == section and "=" in line:
                    k, v = line.split("=", 1)
                    out.setdefault(k.split("[$")[0].strip(), v.strip())
    except OSError:
        pass
    return out


def stem(icon):
    return os.path.splitext(os.path.basename(icon))[0].lower()


# 1. trouver le raccourci Vesktop modifié par KDE
cands = []
for p in glob.glob(os.path.join(glob.escape(DATA), "applications", "**", "*.desktop"), recursive=True):
    e = ini(p, "Desktop Entry")
    blob = " ".join([os.path.basename(p), e.get("Exec", ""), e.get("Name", ""), e.get("StartupWMClass", "")]).lower()
    if "vesktop" in blob:
        # priorité : icône perso, puis nom de fichier "vesktop", puis le plus récent
        cands.append(((stem(e.get("Icon", "")) not in DEFAUT, "vesktop" in os.path.basename(p).lower(), os.path.getmtime(p)), p, e))
if not cands:
    stop("Aucun raccourci Vesktop modifié dans " + os.path.join(DATA, "applications")
         + " : clic droit > Modifier l'application, change l'icône, puis relance.")
cands.sort(key=lambda c: c[0], reverse=True)
_, desk, entry = cands[0]
icon = entry.get("Icon", "")
print("Raccourci KDE : " + desk)
if stem(icon) in DEFAUT:
    stop("Ce raccourci a encore l'icône d'origine (Icon=" + icon + ").")


# 2. retrouver le fichier image derrière le nom d'icône
def resolve(icon):
    if icon.startswith("file://"):
        icon = icon[7:]
    icon = os.path.expanduser(icon)
    if os.path.isabs(icon):
        return icon if os.path.isfile(icon) else None
    name = icon
    if name.lower().endswith((".png", ".svg", ".svgz", ".xpm", ".jpg", ".jpeg", ".webp", ".ico")):
        name = os.path.splitext(name)[0]
    xdg = [DATA] + (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    xdg += [os.path.join(H, ".local/share/flatpak/exports/share"), "/var/lib/flatpak/exports/share", "/usr/share"]
    bases = []
    for b in [os.path.join(H, ".icons")] + [os.path.join(d, "icons") for d in xdg if d]:
        if b not in bases and os.path.isdir(b):
            bases.append(b)
    # thème actuel + ceux dont il hérite (Inherits=), comme le fait KDE
    theme = ini(os.path.join(CFG, "kdeglobals"), "Icons").get("Theme") or "breeze"
    order, queue = [], [theme]
    while queue:
        t = queue.pop(0)
        if t in order:
            continue
        order.append(t)
        for b in bases:
            idx = os.path.join(b, t, "index.theme")
            if os.path.isfile(idx):
                queue += [x.strip() for x in ini(idx, "Icon Theme").get("Inherits", "").split(",") if x.strip()]
                break
    for t in ("breeze", "hicolor"):
        if t not in order:
            order.append(t)

    def size(p):
        # 48x48@2 -> 96, scalable -> le plus grand
        best = 0
        for seg in p.split("/"):
            if seg == "scalable":
                best = max(best, 4096)
            m = re.fullmatch(r"(\d+)(?:x\d+)?(?:@(\d+)x?)?", seg)
            if m:
                best = max(best, int(m.group(1)) * int(m.group(2) or 1))
        return best

    for t in order:
        found = []
        for b in bases:
            d = glob.escape(os.path.join(b, t))
            for pat in ("*", "*/*", "*/*/*"):
                for ext in ("svg", "svgz", "png", "xpm"):
                    found += glob.glob(os.path.join(d, pat, glob.escape(name) + "." + ext))
        if found:
            return max(found, key=lambda p: (size(p), p.endswith(("svg", "svgz"))))
    for b in (os.path.join(DATA, "pixmaps"), "/usr/local/share/pixmaps", "/usr/share/pixmaps"):
        for ext in ("svg", "png", "xpm", "jpg"):
            p = os.path.join(b, name + "." + ext)
            if os.path.isfile(p):
                return p
    # dernier recours
    for tool in ("kiconfinder6", "kiconfinder5"):
        if shutil.which(tool):
            r = subprocess.run([tool, name], capture_output=True, text=True)
            out = r.stdout.strip().splitlines()
            if out and os.path.isfile(out[0]):
                return out[0]
    return None


src = resolve(icon)
if not src:
    stop("Fichier introuvable pour l'icône « " + icon + " ».")
print("Image utilisée : " + src)


# 3. fabriquer tray + trayUnread
tmp = tempfile.mkdtemp(prefix="vesktop-icone-")
RSVG = shutil.which("rsvg-convert")
MAGICK = shutil.which("magick") or shutil.which("convert")


def kind(path):
    with open(path, "rb") as f:
        head = f.read(4096)
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x1f\x8b") or b"<svg" in head or path.lower().endswith((".svg", ".svgz")):
        return "svg"
    return "autre"


base = src
k = kind(src)
if k == "svg":
    base = os.path.join(tmp, "base.png")
    if RSVG:
        subprocess.run([RSVG, "-w", "512", "-h", "512", "-a", "-o", base, src], check=True)
    elif MAGICK:
        subprocess.run([MAGICK, "-background", "none", "-density", "384", src, "-resize", "512x512", base], check=True)
    else:
        stop("Il faut rsvg-convert pour les SVG : sudo pacman -S librsvg")
elif k == "autre":
    base = os.path.join(tmp, "base.png")
    if not MAGICK:
        stop("Format pas géré sans ImageMagick : sudo pacman -S imagemagick")
    subprocess.run([MAGICK, src + "[0]", "-background", "none", base], check=True)

tray, unread = os.path.join(tmp, "tray.png"), os.path.join(tmp, "trayUnread.png")
if RSVG:
    # astuce : on emballe l'image dans un SVG pour pouvoir dessiner la
    # pastille par-dessus avec rsvg-convert seul
    mime = "image/jpeg" if kind(base) == "jpeg" else "image/png"
    with open(base, "rb") as f:
        uri = "data:" + mime + ";base64," + base64.b64encode(f.read()).decode()
    head = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="256" height="256" viewBox="0 0 256 256"><image width="256" height="256" '
            'preserveAspectRatio="xMidYMid meet" xlink:href="' + uri + '"/>')
    dot = '<circle cx="208" cy="208" r="36" fill="#f23f43" stroke="#000000" stroke-width="10"/>'
    for out, extra in ((tray, ""), (unread, dot)):
        svg = os.path.join(tmp, "w.svg")
        with open(svg, "w") as f:
            f.write(head + extra + "</svg>")
        subprocess.run([RSVG, "-o", out, svg], check=True)
elif MAGICK:
    cmd = [MAGICK, base, "-background", "none", "-resize", "256x256", "-gravity", "center", "-extent", "256x256"]
    subprocess.run(cmd + [tray], check=True)
    subprocess.run(cmd + ["-fill", "#f23f43", "-stroke", "#000000", "-strokewidth", "10",
                          "-draw", "circle 208,208 208,172", unread], check=True)
else:
    shutil.copyfile(base, tray)
    shutil.copyfile(base, unread)


# 4. copier dans userAssets (version native et/ou flatpak)
dirs = [os.path.join(CFG, "vesktop"), os.path.join(CFG, "Vesktop"),
        os.path.join(H, ".var/app/dev.vencord.Vesktop/config/vesktop")]
targets = ([d for d in dirs if os.path.isdir(os.path.join(d, "sessionData"))]
           or [d for d in dirs if os.path.isdir(d)] or dirs[:1])
for d in targets:
    ua = os.path.join(d, "userAssets")
    os.makedirs(ua, exist_ok=True)
    shutil.copyfile(tray, os.path.join(ua, "tray"))
    shutil.copyfile(unread, os.path.join(ua, "trayUnread"))
    print("Icône installée dans " + ua)
shutil.rmtree(tmp, ignore_errors=True)

# les vieilles versions de Vesktop n'ont pas userAssets
asars = []
for root in ("/usr/lib/vesktop", "/usr/share/vesktop", "/opt/Vesktop", "/opt/vesktop",
             "/var/lib/flatpak/app/dev.vencord.Vesktop/current",
             os.path.join(DATA, "flatpak/app/dev.vencord.Vesktop/current")):
    for dp, _, fn in os.walk(root, followlinks=True):
        if "app.asar" in fn:
            asars.append(os.path.join(dp, "app.asar"))
if asars and not any(b"userAssets" in open(a, "rb").read() for a in asars):
    stop("Vesktop trop ancien pour les icônes perso, mets-le à jour (sudo pacman -Syu).")


# 5. relancer Vesktop (il ne relit les icônes qu'au démarrage)
def lignee():
    # nos propres processus parents, pour ne pas se tuer soi-même
    s, pid = set(), os.getpid()
    while pid > 1 and pid not in s:
        s.add(pid)
        try:
            with open("/proc/%d/stat" % pid) as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return s


def actifs():
    skip, res = lignee(), []
    for d in os.listdir("/proc"):
        if d.isdigit() and int(d) not in skip:
            try:
                with open("/proc/" + d + "/cmdline", "rb") as f:
                    if b"vesktop" in f.read().lower():
                        res.append(int(d))
            except OSError:
                pass
    return res


if not actifs():
    print("✔ Fini, l'icône sera là au prochain lancement de Vesktop.")
    sys.exit(0)
for sig in (signal.SIGTERM, signal.SIGKILL):
    for p in actifs():
        try:
            os.kill(p, sig)
        except OSError:
            pass
    for _ in range(40):
        if not actifs():
            break
        time.sleep(0.25)
    if not actifs():
        break
time.sleep(0.5)
args = [a for a in shlex.split(entry.get("Exec", "")) if not re.fullmatch(r"%[a-zA-Z]|@@[uf]?", a)]
if not args or not (shutil.which(args[0]) or os.path.isfile(args[0])):
    args = ["vesktop"] if shutil.which("vesktop") else ["flatpak", "run", "dev.vencord.Vesktop"]
subprocess.Popen(args, cwd=H, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, start_new_session=True)
print("✔ Fini, Vesktop relancé avec la nouvelle icône.")
