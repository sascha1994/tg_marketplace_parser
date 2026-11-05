# Обновление через Dockhand

GitHub Actions проверяет изменения и публикует готовый Docker-образ в GitHub Container Registry (GHCR):
- при push в `release/x.y.z` публикуются теги `:x.y.z`, **`:stable`** и `:sha-<commit>`;
- при push в `main` публикуются теги `:latest` и `:sha-<commit>`.

Серверный стек Dockhand должен использовать `docker-compose.yml` из репозитория и загружать уже собранный образ, а не собирать его.

## Настройка автоматического обновления при выходе нового релиза

1. В настройках стека в Dockhand задайте переменные из `.env` приложения и укажите:
   ```env
   PARSER_IMAGE_TAG=stable
   ```
   (По умолчанию в `docker-compose.yml` уже используется `${PARSER_IMAGE_TAG:-stable}`).
2. Убедитесь, что пакет `ghcr.io/sascha1994/tg_marketplace_parser` публичный (или настройте в Dockhand учётные данные GHCR с правом `read:packages`). Каталог, смонтированный в `/app/data`, должен принадлежать пользователю `1000:1000` (`sudo chown -R 1000:1000 /opt/tg_parser/data`) и оставаться постоянным между обновлениями.
3. Выберите один из двух способов автообновления в Dockhand:
   - **Способ А (Расписание / Auto-Update в Dockhand):** включите в настройках стека автообновление образа по расписанию (например, каждые 5–15 минут). Dockhand будет проверять digest тега `:stable` в GHCR и при выходе новой релизной ветки автоматически выполнит `pull` и пересоздаст контейнер.
   - **Способ Б (Мгновенное обновление по Webhook из GitHub Actions):** включите Webhook для стека в Dockhand, скопируйте сгенерированный URL и добавьте его в секреты репозитория GitHub (`Settings → Secrets and variables → Actions → New repository secret`) с именем `DOCKHAND_WEBHOOK_URL`. После каждой успешной сборки образа шаг `Trigger Dockhand webhook` в GitHub Actions автоматически вызовет этот URL, и Dockhand сразу обновит стек.

## Ручная фиксация версии и откат

Если нужно зафиксировать конкретную версию или откатиться на предыдущий релиз:
1. Измените в Dockhand переменную `PARSER_IMAGE_TAG` на нужный номер версии (например, `PARSER_IMAGE_TAG=1.0.3`) или `sha-<commit>`.
2. Выполните Pull и Redeploy стека.
3. Чтобы снова вернуться на автоматическое получение новых релизов, установите `PARSER_IMAGE_TAG=stable`.
