# Teswirler Telegram Bot

[teswirler.com](https://teswirler.com) saýtynyň bildirişlerini Telegram-da almak üçin köp ulanyjyly bot.

**Bot:** [@teswirlerBot](https://t.me/teswirlerBot) · **Interfeýs:** Türkmençe we Русский · **Python:** 3.10+

Her ulanyjy öz hasabyny birikdirýär. Bot täze hatlary, teswirleri, jogaplary, bellenmeleri we beýleki bildirişleri şahsy söhbete iberýär.

## Näme edip bolýar?

- Ähli täze bildirişleri almak we gerek görnüşlerini saýlamak.
- Soňky 5 ýa-da 10 bildirişi gaýtadan soramak.
- Ümsüm sagatlary bellemek, bildirişleri wagtlaýyn duruzmak we dowam etdirmek.
- Bir ýazgynyň ýa-da teswiriň halanmalaryny bir bildirişe birleşdirmek.
- Şahsy hatlara Telegram-dan jogap bermek; alyjy we tekst iberilmezden öň tassyklanýar.
- Şu günüň jemlemesini soramak ýa-da her gün belli wagtda jemleme almak.
- Dili we wagt zolagyny üýtgetmek.
- Hasaby aýyrmak we onuň işjeň sessiýasyny, taryhyny hem nobatyny öçürmek.

Bildirişde wakanyň görnüşi we ulanyjynyň ady aýratyn görkezilýär. Hat ýa-da teswir sitata görnüşinde berilýär; doly teksti saýtda açmak üçin düwme bar. Wagt saýlanan UTC zolagyna görä görkezilýär. Awtorlaryň ýazgylary terjime edilmeýär.

## Boty ulanmak

1. [@teswirlerBot](https://t.me/teswirlerBot) açyň we `/start` iberiň.
2. **Türkmençe** ýa-da **Русский** saýlaň.
3. **Hasaby birikdirmek** düwmesine basyň.
4. Saýtdaky ulanyjy adyňyzy ýa-da e-poçtaňyzy, soňra parolyňyzy aýratyn habar bilen iberiň.
5. Hasap birikdirilenden soň ähli täze bildirişler şol söhbete geler. Öňki taryh awtomatiki iberilmeýär.

Giriş ädimleriniň her biri üçin bäş minut berilýär. Bot täzeden işe başlasa, tamamlanmadyk giriş täzeden başlanýar. Bir saýt hasaby bir Telegram söhbetine birikdirilýär; başga hasaba geçmek üçin ilki häzirki hasaby aýryň.

### Buýruklar

| Buýruk | Näme edýär? |
| --- | --- |
| `/start` | Boty açýar we dil saýladýar |
| `/connect` | Saýtdaky hasaby birikdirýär |
| `/status` | Hasabyň ýagdaýyny görkezýär |
| `/recent` | Soňky 5 ýa-da 10 bildirişi saýladýar |
| `/recent 5` | Soňky 5 bildirişi gaýtadan iberýär |
| `/recent 10` | Soňky 10 bildirişi gaýtadan iberýär |
| `/settings` | Ähli bildiriş sazlamalaryny görkezýär |
| `/filters` | Bildiriş görnüşlerini saýladýar |
| `/quiet 23:00-08:00` | Ümsüm sagatlary belleýär |
| `/quiet off` | Ümsüm sagatlary öçürýär |
| `/grouping` | Halanmalary birleşdirmegi sazlaýar |
| `/summary` | Şu ýerli günüň jemlemesini taýýarlaýar |
| `/digest 21:00` | Gündelik jemlemäniň wagtyny belleýär |
| `/digest off` | Gündelik jemlemäni öçürýär |
| `/pause` | Awtomatiki bildirişleri duruzýar |
| `/resume` | Bildirişleri dowam etdirýär |
| `/language` | Interfeýsiň dilini saýladýar |
| `/timezone +05:00` | Wagt zolagyny belleýär |
| `/disconnect` | Hasaby aýyrmagy tassykladýar |
| `/cancel` | Girişi ýa-da jogap ýazmagy ýatyrýar |
| `/help` | Kömek görkezýär |

Bu amallaryň köpüsi menýudaky düwmeler bilen hem elýeterli.

### Deslapky sazlamalar

- Bildirişleriň ähli görnüşleri açyk.
- Ümsüm sagatlar öçürilen.
- Halanmalary birleşdirmek açyk; garaşylýan wagt 20 sekunt.
- Saýtdaky halanma sanynyň azalmagy ýa-da öň görlen ýagdaýyň gaýtadan gelmegi täze bildiriş döretmeýär. Bot öňki iň ýokary sany we görülen gatnaşyjylary ýatda saklaýar; täze san ösüşi ýa-da täze gatnaşyjy aýratyn barlanýar.
- Gündelik jemleme ýerli wagt bilen **21:00-da** açyk.
- Täze ulanyjynyň wagt zolagy **UTC+5**. `/timezone` arkaly üýtgedip bolýar.

`/recent` we `/summary` el bilen berlen haýyşlardyr: olar pauza we ümsüm sagatlara garamazdan ýerine ýetirilýär. Awtomatiki bildirişler bolsa saýlanan görnüşlere we wagt düzgünlerine eýerýär.

### Hatlara jogap bermek

Şahsy hatyň aşagyndaky **Jogap bermek** düwmesine basyň. Bot saýtyň API-si arkaly söhbeti we alyjyny barlar, soňra jogabyňyzy ýazmagy sorar. Teksti we alyjyny barlap, **Ibermek** düwmesine basyň.

Täze tekst öňki garalamany çalyşýar; öňki tassyklama indi işlemeýär. Ibermezden öň alyjylar ýene barlanýar. Tor näsazlygy sebäpli netijesi näbelli bolsa, bot habary awtomatiki gaýtadan ibermeýär: ilki saýtdaky söhbeti barlamagy soraýar.

### Jemlemeler

`/summary` şu ýerli günüň wakalaryny görnüşleri boýunça sanap, soňky wakalara salgylary görkezýär. Şahsy hatlaryň tekstleri jemlemede sitata edilmeýär.

Gündelik jemleme öňki 24 sagady öz içine alýar we awtomatiki bildirişleriň saýlanan görnüşlerini hasaba alýar. Bot birnäçe gün işlemese, diňe iň soňky möhleti geçen jemleme taýýarlanýar. Hasabat saýtyň bot üçin elýeterli bildirişlerine esaslanýar; ähli hereketleriň doly arhiwi hökmünde kabul edilmeli däl.

## Parollar we maglumatlar

Giriş adaty Telegram şahsy söhbetinde amala aşyrylýar; bu gizlin söhbet däl. Bot parolly habary saýtda giriş barlanmazdan öň öçürýär. Habary öçürip bolmasa, giriş ýerine ýetirilmeýär. Söhbetden öçürmek Telegram-yň ulgamyndan ýa-da enjamlardaky bildirişlerden doly ýok edilendigini aňlatmaýar.

Parol diňe bir giriş synanyşygy wagtynda ýatda ulanylýar. Ol faýla, maglumatlar bazasyna ýa-da žurnala ýazylmaýar. Saýtyň sessiýasy we refresh-cookie **Fernet** bilen şifrlenýär. Şifrleme açary serwerde saklanýar; açara we maglumatlara doly giriş bolan administrator sessiýany açyp biler.

Her ulanyjynyň sessiýasy, taryhy we iberiş nobaty aýratyn katalogda ýerleşýär. Taryhyň we nobatyň SQLite bazasy şifrlenmeýär; oňa katalogyň giriş hukuklary arkaly gorag berilýär. Hakyky bildirişler we Telegram täzelenmeleriniň doly mazmuny žurnala ýazylmaýar.

`/disconnect` işjeň birikmäniň maglumatlaryny öçürýär. Ol saýtdaky beýleki girişleri ýapmaýar. Administratoryň aýratyn döreden ätiýaçlyk nusgalary aýratyn dolandyrylýar.

## Öz botuňyzy işe girizmek

Gerek zatlar:

- Python 3.10 ýa-da has täze wersiýa;
- Telegram-daky [@BotFather](https://t.me/BotFather) arkaly döredilen botuň tokeni;
- Telegram we teswirler.com bilen internet aragatnaşygy.

Adaty ulanyjylara bot tokeni gerek däl. Ol diňe serweri dolandyrýan adam üçin niýetlenendir.

### Linux

Systemd hyzmaty `~/teswirler-bot` katalogyna niýetlenen:

```bash
git clone https://github.com/Pirnazar/teswirler-telegram-bot.git ~/teswirler-bot
cd ~/teswirler-bot
python3 -m venv .venv-public
.venv-public/bin/python -m pip install -r requirements.txt
.venv-public/bin/python bot.py pair
```

`pair` bot tokenini gizlin giriş bilen soraýar. Görkezilen Telegram salgysyny açyp, şol gezeklik `/start` koduny iberiň. Bu ädim administratoryň Telegram söhbetini bellige alýar; saýtdaky hasaby birikdirmek botuň interfeýsinde aýratyn ýerine ýetirilýär.

Soňra:

```bash
.venv-public/bin/python public_bot.py
```

Boty açyp, `/start` arkaly saýtdaky hasabyňyzy birikdiriň.

### Linux-da hemişelik hyzmat

```bash
mkdir -p ~/.config/systemd/user
cp deploy/teswirler-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now teswirler-bot
systemctl --user status teswirler-bot
```

SSH sessiýasy ýapylandan soň hem hyzmatyň işlemegi üçin ulgam administratory ulanyja `loginctl enable-linger` sazlamasyny açyp biler.

```bash
journalctl --user -u teswirler-bot -n 30 --no-pager
systemctl --user restart teswirler-bot
systemctl --user stop teswirler-bot
systemctl --user disable --now teswirler-bot
```

Serwere daşardan giriş gerek däl: bot Telegram-a we saýta özi çykýan HTTPS haýyşlaryny iberýär. Şol bir token bilen bir wagtda diňe bir nusgany işlediň.

### Windows

PowerShell-de:

```powershell
git clone https://github.com/Pirnazar/teswirler-telegram-bot.git teswirler-bot
Set-Location teswirler-bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe bot.py pair
.\.venv\Scripts\python.exe public_bot.py
```

Fon režiminde işe girizmek we duruzmak:

```powershell
powershell -ExecutionPolicy Bypass -File .\Start-Bot.ps1
powershell -ExecutionPolicy Bypass -File .\Stop-Bot.ps1
```

Windows-a girilende awtomatiki başlatmak:

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-Autostart.ps1
powershell -ExecutionPolicy Bypass -File .\Install-Autostart.ps1 -Remove
```

## Maglumatlaryň ýerleşýän ýeri

| Ýer | Mazmun |
| --- | --- |
| `.secrets/telegram.json` | Botuň tokeni we administratoryň birikdiriş sazlamasy |
| `.secrets/public.key` | Sessiýalaryň şifrleme açary |
| `data/public.sqlite3` | Birikmeler, dil, wagt zolagy we ulanyjy sazlamalary |
| `.secrets/public-users/<telegram_id>/<storage_id>/session.enc` | Şifrlenen saýt sessiýasy |
| Şol katalogdaky `notifications.sqlite3` | Şahsy taryh, statistika, jogap düwmeleri we iberiş nobaty |
| `.secrets/pending-logins/` | Tamamlanmadyk girişleriň wagtlaýyn şifrlenen maglumatlary |
| `logs/public.log` | Möçberi çäklendirilen iş žurnaly |

Ätiýaçlyk nusgada maglumatlar bilen birlikde şifrleme açaryny hem goraň. Açar ýitse, ulanyjylar täzeden girmeli bolar. Bu kataloglar, tokenler, sessiýalar, maglumatlar bazalary we žurnallar `.gitignore` arkaly repozitoriýadan çykarylýar.

Öňki ýeke ulanyjyly wersiýanyň maglumatlary bar bolsa, ilkinji açylyşda birikme we nobat täze gurluşa geçirilýär. Öňki faýllar yzyna dolanmak üçin galýar; täze proses olary iş üçin ulanmaýar.

## Taslamanyň gurluşy

```text
bot.py                     Saýtyň we Telegram-yň API müşderileri, bildiriş görnüşi
public_bot.py              Köp ulanyjyly bot, giriş we işleriň dolandyrylyşy
features.py                Süzgüçler, birleşdirmek, taryh we jemlemeler
features_ui.py             Sazlama menýulary we jogap tassyklamasy
i18n.py                    Türkmençe we rusça interfeýs tekstleri
deploy/                    Systemd hyzmaty
docs/                      Tehniki bellikler
test_*.py                  Hyzmatlardan aýry işleýän testler
```

## Testler

```bash
python -m pip install -r requirements.txt
python -m unittest -v
```

Testler wagtlaýyn kataloglary we hyzmatlaryň çalşyrylan nusgalaryny ulanýar. Olar hakyky ulanyjylara habar ibermeýär. Giriş, hasaplaryň aýratynlygy, şifrleme, migrasiýa, wagt zolagy, süzgüçler, jemlemeler we jogaplaryň gaýtadan iberilmezligi barlanýar.

## Çäkler we indiki ädimler

- Saýt her işjeň hasap üçin takmynan 30 sekuntdan bir barlanýar. Bir gezekde iň köp 5000 bildiriş okalýar.
- API saýtyň häzirki web müşderisine görä öwrenildi. Saýtyň üýtgemegi botuň hem täzelenmegini talap edip biler.
- Barlaglaryň arasynda ýiten wakalary ýa-da birleşdirilen bildirişiň ähli aralyk görnüşlerini dikeltmek mümkin däl.
- Telegram habary kabul edenden soň aragatnaşyk kesilse ýa-da netije ýazylmanka proses ýapylsa, bildiriş gaýtalanyp biler. Saýta jogap ibermek şeýle näbellilikde awtomatiki gaýtalanmaýar.
- Google arkaly giriş saýtda bar, ýöne botda entek birikdirilmedi. Munuň üçin saýtyň backend-inde bir gezeklik birikdiriş koduny bermek we çalyşmak gerek. [Tehniki bellikler — rus dilinde](docs/google-login.md).
- Ulanyjylar köpelse, saýtyň özünde waka nobatyny we parol ibermezden birikdirmegi goşmak ýerlikli bolar.

Peýdaly çeşmeler: [Telegram Bot API](https://core.telegram.org/bots/api) · [Fernet](https://cryptography.io/en/latest/fernet/)
