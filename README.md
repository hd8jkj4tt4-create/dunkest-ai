# Dunkest AI Points Maximizer

App locale FastAPI + HTML/CSS/JavaScript per sincronizzare i dati pubblici Dunkest NBA e ottimizzare una rosa in base al budget inserito.

## Avvio su Windows

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```
Apri http://127.0.0.1:8000.

## Scansione live Dunkest

Il pulsante **Sincronizza fonte live** interroga la tabella pubblica delle statistiche NBA di Dunkest (`/api/stats/table`), senza credenziali. La stagione corrente è impostata a `season_id=29` (NBA 2026/27, come indicato dalla pagina pubblica delle statistiche).

Per una futura stagione puoi impostare prima dell'avvio `DUNKEST_SEASON_ID`. Per usare un endpoint JSON alternativo, imposta `DUNKEST_DATA_URL`.

La sincronizzazione valida il numero e i campi dei giocatori prima di salvare: se il sito cambia formato o la rete non è raggiungibile, conserva il database precedente e mostra l'errore. La tabella pubblica fornisce quotazione e statistiche, non necessariamente proiezioni future o stato medico aggiornato; in assenza di proiezioni l'ottimizzatore usa la media PDK per partita come proxy.

## Budget e ottimizzazione

Inserisci i crediti nel campo **Crediti disponibili** prima di generare la squadra. Il valore viene inviato all'API ad ogni calcolo. I vincoli di roster iniziali sono 10 giocatori: 2 C, 4 F e 4 G, **massimo 2 giocatori per squadra NBA**. L'allenatore è escluso da questo limite: sono quindi validi 2 giocatori + l'allenatore della stessa squadra. Verifica che i vincoli corrispondano al regolamento della modalità/giornata che stai usando.

Il motore massimizza il punteggio stimato e, a parità di punteggio, il budget utilizzato. Se la fonte non fornisce una proiezione, usa medie/forma disponibili come proxy: non è una previsione garantita.

## Allenatore

In modalità Classica Dunkest si compone **10 giocatori + 1 allenatore con lo stesso budget (95 crediti)**. Il pulsante **Sincronizza allenatori** usa automaticamente i dati live Dunkest e li confronta con più fonti:

- **Dunkest live API** (`fantaking-api.dunkest.com`): quotazione corrente, giornata, avversario, media fantasy e ultime 5 partite dell'allenatore
- **Dunkest listone** (foglio Google pubblicato da Dunkest): fallback per nome/squadra/crediti se l'API live o il token non sono disponibili
- **ESPN** (API classifiche): vittorie/sconfitte e punti fatti/subiti
- **Basketball-Reference**: incrocio del record e carriera dell'allenatore
- **NBA.com** (API stats): confronto aggiuntivo, usato solo se raggiungibile (alcune reti lo bloccano; in tal caso le altre fonti restano attive)

Il punteggio atteso applica la tabella ufficiale Dunkest (vittoria/sconfitta con scarti, bonus/malus sui punti fatti e subiti, solo se i dati PPG sono disponibili) e il **coefficiente di difficoltà avversario** (tabella ufficiale 0,70–1,60), calcolato sulla quotazione reale dell'allenatore avversario della giornata. La proiezione finale è **60% media live Dunkest + 40% modello regolamentare**; senza dati live resta il solo modello. L'ottimizzatore sceglie **esattamente un allenatore**, conteggia i suoi crediti nel budget totale e lo somma al punteggio dei 10 giocatori, ma **non** lo include nel limite di 2 giocatori per squadra. La casella **Allenatore** nella sezione team permette di disattivarlo e tornare al solo roster.

### Token Dunkest e aggiornamento automatico

Il token serve solo per scoprire gli ID degli allenatori nel listone autenticato; profilo e match-log sono pubblici. Il primo sync salva gli ID nel database, quindi i successivi funzionano anche se il token scade. Per abilitare anche il rinnovo automatico del token, aggiungi al file `.env`:

```dotenv
DUNKEST_TOKEN=...
DUNKEST_EMAIL=tua-email
DUNKEST_PASSWORD=tua-password
DUNKEST_GAME_ID=1
```

Con `DUNKEST_EMAIL` e `DUNKEST_PASSWORD` impostati, se il token manca o scade il collector effettua il login e continua da solo. Senza credenziali, dopo il primo sync il live continua a funzionare tramite gli ID salvati; in caso di fallimento totale viene usato il listone statico. Il file `.env` è ignorato da Git.

## CSV

È possibile importare un CSV con intestazioni `id,name,team,role,price,projection,avg_points,last5,status`. Ruoli: `C`, `F`, `G`.

## API

- `GET /api/health`
- `GET /api/players`
- `POST /api/players/sync`
- `POST /api/players/import-csv`
- `GET /api/coaches`
- `POST /api/coaches/sync`
- `POST /api/optimizer/generate` con JSON `{"budget": 95, "include_coach": true}`

La scansione giocatori usa esclusivamente la tabella pubblica. La scansione allenatori usa l'API Dunkest: il token (o le credenziali opzionali) serve solo a leggere il listone della propria lega per scoprire gli ID degli allenatori; quotazioni, avversari e punteggi vengono poi letti da endpoint pubblici. Nessun dato privato viene salvato oltre a nome/squadra/quotazione pubblici degli allenatori.

## Pubblicazione online (Render, gratis)

1. Crea un account gratuito su https://render.com (non serve la carta di credito).
2. Dashboard → **New** → **Web Service** → **Build and deploy from a Git repository** → collega il repository GitHub di questo progetto.
3. Impostazioni del servizio:
   - Name: `dunkest-ai`
   - Region: **Frankfurt**
   - Runtime: **Python 3**
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
   - Plan: **Free**
4. **Create Web Service**. Al termine l'app è online su `https://dunkest-ai-xxxx.onrender.com`, con HTTPS, accessibile da chiunque abbia il link.

Funzionamento in cloud:

- I dati si aggiornano da soli all'avvio quando sono vecchi (>3h per i giocatori, >12h per gli allenatori) e con i pulsanti di sincronizzazione (massimo una richiesta ogni 5 minuti per tipo, per non disturbare i siti fonte).
- Il free plan si spegne dopo 15 minuti di inattività: al primo accesso ci vuole circa un minuto di risveglio.
- Il database è incluso nel repository come snapshot iniziale: se un sito fonte blocca le richieste dalla rete del cloud (alcuni lo fanno), l'interfaccia resta comunque utilizzabile con l'ultimo listone noto.
- Su Render il file `.env` non viene caricato: imposta `DUNKEST_TOKEN` (o `DUNKEST_EMAIL` + `DUNKEST_PASSWORD`) tra le **Environment Variables** del servizio. Lo snapshot del database include già gli ID degli allenatori correnti, quindi il live funziona anche senza token finché la lista allenatori non cambia.
