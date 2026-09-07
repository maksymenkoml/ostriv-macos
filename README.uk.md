[English](README.md) | Українська

# Ostriv на Mac (Apple Silicon)

[Ostriv](https://store.steampowered.com/app/773790/Ostriv/) падає під час запуску в
CrossOver. Цей проєкт це виправляє.

## Гра

**[Завантажте останній ZIP для гравця](https://github.com/maksymenkoml/ostriv-macos/releases/latest/download/ostriv-macos-player.zip).**

1. Розпакуйте ZIP.
2. Відкрийте Terminal у розпакованій теці й виконайте `python3 patch.py`.
3. Оберіть **Install** і дотримуйтесь підказок.
4. Відкрийте **Ostriv (patched)** з `~/Applications/CrossOver`.

Потрібні Mac на Apple Silicon, [CrossOver](https://www.codeweavers.com/crossover) і Ostriv,
встановлений через Steam у пляшці (bottle) CrossOver. Не запускайте гру кнопкою **Play** у
Steam; користуйтеся пропатченим лаунчером.

Щоб прибрати виправлення, знову відкрийте патчер і оберіть **Restore**. Файли гри не
змінюються, встановлення повністю зворотне.

## Усунення проблем

| Симптом | Одна дія |
| --- | --- |
| **Package: FAILED** | Ще раз завантажте й розпакуйте останній ZIP для гравця. |
| **CrossOver або гру не знайдено** | Встановіть Ostriv у Steam-пляшку CrossOver і запустіть патчер знову. |
| **Тайм-аут Steam** | Повністю закрийте CrossOver, відкрийте його знову й запустіть патчер ще раз. |
| **Помилка графічного контексту** | Повністю закрийте CrossOver, потім знову відкрийте пропатчений лаунчер. |
| **Кілька копій CrossOver** | Повністю закрийте всі копії CrossOver і Steam, завершіть залишені процеси `wine`/`wineserver` у Моніторингу системи, потім відкрийте лише актуальний CrossOver і запустіть патчер знову. |
| **Неочікувана помилка** | Виконайте `python3 patch.py --diagnose` і додайте лог інсталятора до звіту про помилку. |

Лог інсталятора: `~/Library/Logs/ostriv-macos/install.log`

## Що робить патчер

Ostriv потребує OpenGL 4.3, а macOS надає лише 4.1. Патчер встановлює власний графічний
драйвер Mesa, який через CrossOver виводить графіку на Apple Metal, застосовує потрібні
налаштування і створює лаунчер **Ostriv (patched)**. Він також вимикає мультисемплінг, який
на Mac завалює гру, і вмикає безрамковий повноекранний режим.

Повне технічне пояснення є в [docs/technical.uk.md](docs/technical.uk.md). Перевірено:
Apple M5 Max · CrossOver 26.2 · Ostriv 0.5.9.58.

## Розробка

Контриб'юторам репозиторію потрібен Git LFS, щоб DLL у робочій копії були справжніми
файлами, а не вказівниками:

```bash
git clone https://github.com/maksymenkoml/ostriv-macos
cd ostriv-macos
git lfs install
git lfs pull
python3 -m unittest discover -s tests -v
```

Зібрати й перевірити ZIP для гравця (без Git) локально:

```bash
python3 scripts/build-release.py --output dist/ostriv-macos-player.zip
```

Щоб опублікувати реліз для гравців, змініть `__version__` в `ostriv_macos/__init__.py` на
наступну версію `X.Y.Z` у pull request. Коли ця зміна потрапить у `main` і повний workflow
Test пройде, GitHub Actions створить відповідний тег, збере ZIP для гравця й опублікує
GitHub Release. Перезапускайте **Publish player ZIP** вручну лише для відновлення невдалої
публікації; наявний реліз безпечно пропускається.

Щоб перезібрати сам драйвер, дійте за [docs/technical.uk.md](docs/technical.uk.md) і
запустіть `scripts/build-driver.sh` з робочої копії репозиторію.

## Подяки та ліцензія

- [Mesa 3D](https://www.mesa3d.org/) (MIT): драйвер `d3d12` від Microsoft; базова збірка для
  Windows від [pal1000/mesa-dist-win](https://github.com/pal1000/mesa-dist-win).
- D3DMetal (Apple Game Porting Toolkit, постачається з CrossOver).
- Патч Mesa у цьому репозиторії поширюється за MIT, як і сама Mesa.

Проєкт не пов'язаний з розробником Ostriv, Євгенієм Гребенюком. Купуйте гру в
[Steam](https://store.steampowered.com/app/773790/Ostriv/).
