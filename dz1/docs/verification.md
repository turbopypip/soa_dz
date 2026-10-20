# Проверка каркаса

Проверено 2 октября 2026 года на Docker Engine 27.5.1, Docker Compose 2.32.4, Linux/arm64 в Docker Desktop. Это проверка реально запущенного контейнера, не только анализ Dockerfile.

Для проверки использовано отдельное имя Compose-проекта `soa-dz1-check`. Команды ниже выполняются из `dz1` (при проверке из корня передавался `-f dz1/docker-compose.yml`).

При первой попытке завис `docker-credential-desktop`. Повторная сборка выполнена с временным `DOCKER_CONFIG` без авторизации и `DOCKER_HOST`, указывающим на сокет Docker Desktop. Образ Python публичный; пользовательская конфигурация Docker не менялась. Это особенность окружения проверки, для обычного запуска достаточно команд из README.

Команды:

```bash
docker compose -p soa-dz1-check up --build -d --wait
docker compose -p soa-dz1-check ps
curl --fail-with-body -i http://localhost:8080/health
curl -i http://localhost:8080/orders
docker inspect soa-dz1-check-orders-1 --format '{{.State.Health.Status}} user={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}}'
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

После проверки тестовый контейнер и его сеть удалены командой `docker compose -p soa-dz1-check down`.
