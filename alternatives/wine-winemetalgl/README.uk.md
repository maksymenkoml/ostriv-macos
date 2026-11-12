[English](README.md) | Українська

# Альтернатива: Ostriv на Mac без CrossOver (Wine 11 + WineMetalGL)

Безкоштовна експериментальна альтернатива основному патчеру цього репозиторію, який працює з CrossOver.

**Зв'язка:** [Wine 11.0 (збірка Gcenx)](https://github.com/Gcenx/macOS_Wine_builds), x86_64 під Rosetta
+ [WineMetalGL 3.9.0](https://github.com/metalsharp/WineMetalGL) (OpenGL 4.6 напряму поверх Metal)
+ Windows-Steam в окремому Wine-префіксі.

Ostriv потребує OpenGL 4.3, а macOS надає лише 4.1. WineMetalGL перекладає OpenGL одразу в Metal,
тож тут не потрібні ні CrossOver, ні D3DMetal, ні пропатчена Mesa з основного патчера.
З цього репозиторію використовується лише `assets/settings.data` (MSAA вимкнено).

## Стан
Перевірено на MacBook Pro M1 (8 ГБ), macOS 27.2, Ostriv 0.5.9.65 (Steam), листопад 2026:

- ✅ гра запускається, завантажує мапу, у неї можна грати;
- ⚠️ **низький FPS** на M1 / 8 ГБ. Головний потік гри завантажує одне ядро на ~100% (гра, Wine
  і WineMetalGL виконують x86-код через Rosetta), а гра (~5–6 ГБ разом із текстурами, пам'ять
  спільна) і Wine/Steam (~2 ГБ) не вміщаються у 8 ГБ. На потужнішому Mac із більшою пам'яттю має
  бути краще;
- ⚠️ вікно Windows-Steam під цим Wine чорне, тому вхід іде через командний рядок (`steam.exe -login`);
  лаунчер питає пароль прихованим введенням і ніде його не зберігає.

Якщо у вас є CrossOver, основний патчер краще перевірений і швидший.

## Встановлення
Потрібні Mac на Apple Silicon, ~5 ГБ на диску й Ostriv, куплений у Steam.

```bash
cd alternatives/wine-winemetalgl
./setup.sh
```

Усе встановлюється в `~/Games/Ostriv` (іншу теку можна задати: `OSTRIV_HOME=/шлях ./setup.sh`).
Скрипт завантажує зафіксовані версії Wine, WineMetalGL і Steam, перевіряє SHA-256,
створює префікс і додає маніфест Ostriv, щоб Steam сам завантажив гру. Повторний запуск безпечний.
Поза обраною текою нічого не встановлюється.

## Гра
1. Закрийте інші програми — пам'яті мало.
2. Двічі клацніть **Play Ostriv.command** у `~/Games/Ostriv` (або `~/Games/Ostriv/play-ostriv.sh`).
3. Введіть логін і пароль Steam і підтвердіть вхід у мобільному застосунку Steam.
   Вікно Steam лишається чорним — не чіпайте його.
4. Під час першого запуску скрипт дочекається, поки Steam завантажить Ostriv (~560 МБ), і запустить гру.

**Не запускайте** гру кнопкою «Грати» у Steam — лише через скрипт.

## Про що дбає лаунчер
- **Гру треба запускати через ~40 с після входу в Steam.** Якщо раніше — Steam не реєструє процес
  (немає `AppID 773790 adding PID` у `Steam/logs/gameprocess_log.txt`), `SteamApps()` повертає NULL
  і гра падає одразу після завантаження інтерфейсу (`read 0 at ostriv.exe+0x359a77`).
- **Watchdog Steam вбиває Steam при нестачі пам'яті** (`CSteamEngine::BMainLoop appears to have
  stalled > 15 seconds`). Unix-процес може лишитися висіти, тож живість перевіряється через
  `wine tasklist`; лаунчер за потреби перезапускає Steam.
- `steam.exe -shutdown` *запускає* Steam, якщо він не запущений, тому викликається лише коли Steam живий.

## Глухі кути
Див. таблицю в [README.md](README.md#dead-ends-so-nobody-repeats-them).

## Видалення
Видаліть `~/Games/Ostriv`.
