# BlaBlaCar BR — contrato da API interna

Levantado por observação do app web em 17/08/2026. É API **interna, não documentada** —
pode mudar sem aviso. Ver [blablacar.py](blablacar.py) para a implementação.

## Barreiras

| Camada | O que é | Como passa |
|---|---|---|
| DataDome | WAF no edge; bloqueia por fingerprint TLS/HTTP2 | `curl_cffi` com `impersonate="chrome"`. Sem isso: **403** em tudo, até `/robots.txt` |
| Auth do edge | `UnauthorizedException: no authentication and resource is not public` | `Authorization: Bearer <applicationToken>` |
| Validação de cliente | `constraint_valid_client / clientTypeAndVersion` | header `x-client: SPA|1.0.0` |

## 1. Obter o token anônimo

O servidor SSR faz `client_credentials` e injeta o resultado no HTML. O `client_secret`
nunca chega ao browser, mas o **token pronto** sim:

```
GET https://www.blablacar.com.br/search?...
```

No HTML: `window["__INFRASTRUCTURE__authentication"]` →
`.authentication.applicationToken.token.accessToken` (UUID, ex. `93ecbc24-...`).

⚠️ Esse blob é objeto **JS**, não JSON: contém `undefined` literal. Precisa sanitizar
antes de `json.loads` (ver `_strip_js_undefined`).

Um token serviu 30+ buscas sem reemissão. Não declara expiração — o cliente renova a
cada 20 min e retenta uma vez em caso de 401.

## 2. Buscar viagens

```
GET https://edge.blablacar.com.br/trip/search/v9
```

Headers: `authorization: Bearer <token>`, `x-client: SPA|1.0.0`, `x-locale: pt-BR`,
`x-currency: BRL`, `x-visitor-id: <uuid>`, `x-correlation-id: <uuid por request>`.

Parâmetros (query achatada, notação de bracket para o mapa `f`):

| Param | Obrigatório | Notas |
|---|---|---|
| `supply` | **sim** | `ALL` \| `CARPOOLING` \| `BUS` \| `TRAIN` |
| `departure_date` | — | `YYYY-MM-DD` |
| `f[fc]` / `f[tc]` | — | coordenadas `"lat,lng"` de origem/destino |
| `from_address` / `to_address` | — | rótulo textual |
| `from_place_id` / `to_place_id` | — | alternativa às coordenadas |
| `requested_seats` | — | inteiro ≥ 1 |
| `return_date` | — | ida e volta |
| `from_cursor` | — | **paginação** (não `cursor`) |
| `search_uuid` | — | omitir no 1º request; repassar nos seguintes |
| `f[sort]` | — | `dep_time:asc`, `price:asc`, `duration:asc`, `dep_dist:asc`, `arr_dist:asc` |
| `f[carrier_*]` | — | filtro por empresa, ex. `f[carrier_luxor]=true` |

### ⚠️ O resultado depende do `x-visitor-id`

O mesmo request, no mesmo instante, devolve listas **de tamanhos diferentes** conforme o
`x-visitor-id`. Cada identidade cai numa cópia do índice, e as cópias têm graus de
atualização diferentes. Medido em 19/08/2026, Balneário Camboriú → Blumenau, 21/08:

| Amostra | Resultado |
|---|---|
| 6 requests, mesmo `x-visitor-id` | 15, 15, 15, 15, 15, 15 |
| 6 requests, só trocando o `x-visitor-id` | 17, 15, 17, **34**, 15, **34** |
| 20 requests, `x-visitor-id` novo em cada | 10× 15, 7× 17, 3× 34 |
| 1 `x-visitor-id` fixo, 33 amostras em 17 min | 15 nas 33 |

Propriedades verificadas:

- **Determinístico por identidade**: o mesmo `x-visitor-id` cai sempre na mesma cópia —
  por pelo menos 17 minutos seguidos, enquanto outras identidades viam 17 e 34.
- **Aninhado**: o conjunto menor está contido no maior. A união de 10 tentativas nunca
  passou do maior conjunto isolado.
- **As caronas "a mais" são reais**: mesma rota e mesma data (conferido no `gtm` de cada
  item), todas respondendo no `/ride/v3` com motorista, nota e taxa de cancelamento.
- **Não é raio de busca**: a distância média do ponto de partida é a mesma nos dois
  conjuntos (2,2 km nas compartilhadas, 1,9 km nas exclusivas do conjunto maior).
- A contagem declarada (`tabs[].count`, `summary_header.number_of_results` e
  `tracking...braze.NumberOfResults`) acompanha a cópia que respondeu — serve como
  medida de quão completa ela é, sem precisar paginar.

Consequência prática: **um cliente que fixa o `x-visitor-id` fica preso a uma cópia** e
pode não enxergar metade do inventário por horas. Por isso `blablacar.py` sorteia uma
identidade nova a cada tentativa e junta os resultados (ver `SEARCH_ATTEMPTS`).

O header é obrigatório: sem ele, ou vazio, a API responde
`400 constraint_not_blank / X-Visitor-Id`.

### Resposta

```
search_uuid
tabs[]                     -> {label, supply, count}   # contagem total por modal
facets                     -> filtros disponíveis + queryParam de cada um
results_content.results
  ├─ search_items[]        -> alguns têm .trip; outros são header/CTA/destaque
  ├─ pagination.next_cursor-> base64 de "page=N"; null na última página
  └─ map.trips[]
```

Cada `search_items[].trip`:

```
multimodal_id  {source: CARPOOLING|PRO_PARTNER, id, pro_partner_id}   # id único
travel_details
  ├─ itinerary {departure{primary_location,time,coordinates}, arrival{...},
  │             total_duration.label, is_overnight, has_connection}
  ├─ amenities[] {id, label}
  └─ provider  {supply.type: CARPOOL|BUS,
                profiles[] -> {driver{name,avatar,status}} ou {carrier{name,logo}}}
price
  ├─ prefix              # classe do assento: "Convencional", "Leito", ...
  ├─ tokenized_price[]   # {token_type: CURRENCY|INTEGER|FRACTION, token}
  └─ discount            # {value: "-15%", tokenized_original_price[], tag}
```

⚠️ Camelo vs snake: o HTML SSR traz o mesmo payload em **camelCase**
(`travelDetails`, `tokenizedPrice`, `type`/`value`); o edge devolve **snake_case**
(`travel_details`, `tokenized_price`, `token_type`/`token`). Não misture.

## 3. Detalhe da carona + reputação do motorista

```
GET https://edge.blablacar.com.br/ride/v3
    ?source=CARPOOLING&id=<multimodal_id.id>&requested_seats=1
    (&partner_id=<pro_partner_id> quando source=PRO_PARTNER)
```

Os sinais de confiança **não vêm na busca** — só aqui. Um request por carona.

`trip_conditions[].driver.profile`:

```
display_name, thumbnail
verification_status.code        # VERIFIED_IDENTITY
profile_action.id              # UUID do motorista (chave p/ histórico próprio)
info[]                         # array heterogêneo, uma chave por item:
  ├─ super_driver       {title, description}
  ├─ verification_status{label}
  ├─ cancellation_rate  {code, label, explanation_*}   <-- o que importa
  └─ driver_message     {message, expand_action_title}
preferences[]
```

`cancellation_rate.code` observado: **`NEVER`** ("Nunca cancela caronas"),
**`RARELY`** ("Raramente cancela caronas"), **`SOMETIMES`** ("Cancela caronas às vezes").
Presente em **27 de 30** caronas amostradas — motorista sem histórico não tem o campo.

`tracking.on_load.braze.data` traz de bônus, já pronto:

| Campo | Exemplo |
|---|---|
| `RideOwnerRating` | `"4,74"` / `"0"` se não avaliado |
| `IsSuperDriver` | `true` / `false` |
| `IsDriverIdentityVerified` | `true` / `false` |
| `NumberOfChanges` | `0` (conexões) |
| `RideOwnerName`, `RideOwnerPictureURL` | — |

`call_to_action.action.book.approval_mode`: **`MANUAL`** vs `AUTOMATIC`. Em MANUAL o
motorista precisa aceitar o pedido — na amostra, 30/30 eram MANUAL.

Perfil público (mais fundo) fica em `/user/show/:userId` no site, usando o
`profile_action.id`. O esquema do app cita ainda `RIDES_PUBLISHED`, `RESPONSE_TIME`
e `RATING` como estados de perfil.

## 4. Resolver cidade → coordenada

```
GET /geocode/single?address=São Paulo&locale=pt-BR
    -> {place_id, place{name,city,latitude,longitude,country_code}, viewport}

GET /location/suggestions?query=São Paulo        # autocomplete; param é `query`, não `q`
    -> [{id, address, main_text, secondary_text, precise, types}]
```

## Medições

- Latência: **~0,7 s** por request (média de 30).
- Rota São Paulo→Rio, 2026-08-22: **47 viagens em 5 requests / 5 s** (bate com `tabs.count`).
- 30 buscas sequenciais (5 rotas × 6 datas, 1 token, ~0,3 s de intervalo): **30/30 = 200**, zero rate-limit.
- A página SSR sozinha entrega **só a 1ª página** (10 de 47) — os params de cursor na URL
  do site são ignorados. Paginação só pelo edge.

## Alternativa oficial

`https://public-api.blablacar.com/api/v3/` — precisa de API key
([solicitar](https://support.blablacar.com/hc/en-gb/articles/360014200220--How-to-use-BlaBlaCar-search-API-)),
cota inicial de 1000 req/dia. Sem key retorna `401 {"message":"No API key found in request"}`.
Cobre **só carpooling** — não traz o inventário de ônibus, que aqui é ~94% dos resultados.
