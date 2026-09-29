# Проверка каркаса

Проверено 29 сентября 2026 года на Docker Engine 27.5.1, Docker Compose 2.32.4, Linux/arm64 в Docker Desktop. Это проверка реально запущенного контейнера, не только анализ Dockerfile.

Из каталога `dz1` выполнены:

```bash
docker compose up --build -d --wait
docker compose ps
curl --fail-with-body -i http://localhost:8080/health
curl -i http://localhost:8080/orders
docker inspect dz1-orders-1 --format '{{.State.Health.Status}} user={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}}'
```

Результат:

| Проверка | Фактический результат |
| --- | --- |
| Сборка и запуск Compose | Успешно, код завершения 0 |
| Встроенный Docker health-check | `healthy` |
| `GET /health` через опубликованный порт | `HTTP/1.0 200 OK`, `Content-Type: application/json`, тело `{"status":"ok"}` |
| `GET /orders` | `404 Not Found`, тело `{"error":"not found"}`: бизнес-маршрут отсутствует |
| Пользователь контейнера | `10001:10001` |
| Корневая файловая система | `readonly=true` |

Исходник C4 обработан Graphviz без ошибок. SVG проверен XML-парсером, изображение просмотрено визуально. Проектируемые бизнес-потоки не тестировались: по условию задания они не реализованы.
