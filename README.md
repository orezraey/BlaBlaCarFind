# BlaBlaCarFind

Bot de Telegram que monitora uma rota no BlaBlaCar Brasil e avisa quando aparece
uma **carona nova**, já com os sinais de confiança do motorista — nota, taxa de
cancelamento e SuperDriver.

## Instalação

```bash
pip install -r requirements.txt
```

Crie o bot com o [@BotFather](https://t.me/BotFather) e guarde o token. Depois copie o
arquivo de exemplo:

```bash
copy .env.example .env
```

Abra o `.env` e preencha o token:

```ini
TELEGRAM_BOT_TOKEN=123456789:AA...seu_token_aqui
```

E rode:

```bash
python bot.py
```

Sem token o bot para na hora com instruções, em vez de falhar de forma obscura.

### Configuração

Tudo vem do `.env` — ou de variáveis de ambiente, que têm precedência sobre o arquivo
(prático para systemd, Docker ou CI, onde o `.env` nem precisa existir).

| Variável | Padrão | Para que serve |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **Obrigatória.** Token do @BotFather |
| `POLL_MINUTES` | `15` | Intervalo entre as checagens |
| `BLABLACARFIND_DB` | `blablacarfind.db` | Caminho do banco; útil para deixá-lo fora do repositório |

## Comandos

| Comando | O que faz |
|---|---|
| `/monitorar` | Cadastra uma rota: origem → destino → data, escolhendo a cidade numa lista |
| `/rotas` | Lista o que está sendo monitorado |
| `/checar` | Força uma checagem agora, sem esperar o ciclo |
| `/parar [nº]` | Encerra o monitoramento de uma rota |

Ao cadastrar, o bot faz um **baseline**: as caronas que já existem são marcadas como
vistas e não geram notificação. Só o que aparecer depois é avisado. Quando a data da
viagem passa, a rota é encerrada automaticamente.

## Como a notificação fica

```
🚗 Nova carona encontrada
Campinas - SP → São Paulo, SP · qui 20/ago

🕐 05:20 → 06:30  (1h10)
📍 Distrito Industrial → São Paulo
💰 R$ 34,00

👤 Daniel
⭐ 4,77 · ✅ verificado
🟡 Cancela caronas às vezes
🚙 VOLKSWAGEN POLO - Cinza
⚠️ A reserva só vale depois que o motorista aprovar

💬 Pouco espaço para mala, consultar antes
                                    [Ver no BlaBlaCar]
```

Selos de cancelamento: 🟢 `NEVER`/`RARELY` · 🟡 `SOMETIMES` · 🔴 `OFTEN`/`ALWAYS` ·
⚪ sem histórico. Motorista sem histórico **não** vira verde — é uma categoria própria.

Nada é filtrado: todas as caronas novas são notificadas, com o risco visível. Filtrar
poderia esconder a única carona do dia.

## Arquitetura

| Arquivo | Papel |
|---|---|
| [blablacar.py](blablacar.py) | Cliente HTTP da API interna: busca, detalhe da carona, geocoding |
| [storage.py](storage.py) | SQLite: rotas monitoradas e chaves das viagens já vistas |
| [bot.py](bot.py) | Handlers do Telegram e o ciclo de vigilância |
| [API.md](API.md) | Contrato da API interna levantado por engenharia reversa |

Um único processo. O banco (`blablacarfind.db`) é criado ao lado do código.

### Dois pontos que não são óbvios

**Deduplicação não pode usar o ID da viagem.** O `multimodal_id.id` é um blob cifrado
que **muda a cada sessão** — dois processos consultando a mesma carona recebem ids
diferentes. Usar isso como chave faria o bot notificar a rota inteira a cada reinício.
Por isso `Trip.natural_key` é um hash de horário + locais + motorista + duração, que
se manteve idêntico entre sessões nos testes. O preço fica fora de propósito: promoção
não transforma a carona em viagem nova.

**A marcação de "visto" acontece depois do envio.** Se o Telegram falhar, a carona é
tentada de novo no ciclo seguinte em vez de sumir silenciosamente.

## Custo de tráfego

Cada checagem faz 1 request por página de resultado (10 caronas por página), mais
1 request por carona nova (o detalhe do motorista só existe lá).

| Rota | Caronas | Requests/checagem | Por dia a cada 15 min |
|---|---|---|---|
| São Paulo → Rio | ~3 | 1 | ~96 |
| São Paulo → Belo Horizonte | ~5 | 1 | ~96 |
| Campinas → São Paulo | ~103 | ~11 | ~1.056 |

Rotas de commuter (Campinas→SP) são bem mais caras que rotas intermunicipais longas.
Se for monitorar várias rotas movimentadas, considere subir `POLL_MINUTES`.

Latência medida: ~0,7 s por request. As chamadas são serializadas por um lock e há
2 s de pausa entre rotas, para o tráfego não sair em rajada.

## Limitações

- **API interna, não documentada.** Pode mudar sem aviso. Se a busca começar a falhar,
  compare com o [API.md](API.md) — provavelmente mudou nome de campo ou de parâmetro.
- **Depende de impersonação TLS.** O `curl_cffi` com `impersonate="chrome"` é o que
  passa pelo DataDome. Vale fixar a versão do pacote.
- **Só caronas.** Ônibus não tem motorista nem taxa de cancelamento; o cliente suporta
  (`supply="ALL"`), mas o bot monitora apenas `CARPOOLING`.
- **Raspagem provavelmente contraria os Termos de Uso do BlaBlaCar.** Vale conferir
  antes de expor isso para outras pessoas ou aumentar o volume.
