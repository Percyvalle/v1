"""Тесты cigilbot/content/wordlist.py: нормализация под словарное сравнение."""

from __future__ import annotations

from cigilbot.content.wordlist import normalize_for_content_match


class TestObfuscationBypass:
    def test_plain_word_unchanged(self) -> None:
        assert normalize_for_content_match("привет") == "привет"

    def test_uppercase_folds_to_lowercase(self) -> None:
        assert normalize_for_content_match("ПРИВЕТ") == "привет"

    def test_hyphen_separated_letters_collapse(self) -> None:
        assert normalize_for_content_match("п-р-и-в-е-т") == "привет"

    def test_dot_separated_letters_collapse(self) -> None:
        assert normalize_for_content_match("п.р.и.в.е.т") == "привет"

    def test_underscore_separated_letters_collapse(self) -> None:
        assert normalize_for_content_match("п_р_и_в_е_т") == "привет"

    def test_leetspeak_digits_become_letters(self) -> None:
        assert normalize_for_content_match("пр1вет") == "привет"

    def test_latin_homoglyphs_fold_to_cyrillic(self) -> None:
        # Латинская "e" вместо кириллической "е" — визуально неотличимо.
        assert normalize_for_content_match("привет") == "привет"

    def test_combined_obfuscation(self) -> None:
        assert normalize_for_content_match("п-р-1-в-е-т") == "привет"


class TestMultiLetterChunkObfuscation:
    """Обход не только по одной букве, но и частями слова — "пи-д-р",
    "пи.д.р" (пользователь 2026-08-13: "слово пидр (ПИ Д Р)"). Дефис/точка/
    подчёркивание внутри слова редки в обычной русской речи, поэтому для
    них снято ограничение на длину частей (в отличие от пробела, см.
    TestFalsePositiveGuards ниже)."""

    def test_two_letter_chunks_collapse(self) -> None:
        assert normalize_for_content_match("пи-д-р") == "пидр"

    def test_dot_separated_chunks_collapse(self) -> None:
        assert normalize_for_content_match("пи.д.р") == "пидр"

    def test_underscore_separated_chunks_collapse(self) -> None:
        assert normalize_for_content_match("пи_д_р") == "пидр"

    def test_mixed_chunk_sizes_collapse(self) -> None:
        assert normalize_for_content_match("пид-о-р") == "пидор"


class TestFalsePositiveGuards:
    def test_hyphenated_word_now_collapses_by_design(self) -> None:
        # Побочный эффект принятого компромисса (2026-08-13): дефис внутри
        # слова больше не защищён от схлопывания независимо от длины
        # частей — "по-русски" тоже становится "порусски". Это влияет
        # только на internal-сравнение со словарём в normalize_for_content_
        # match, не на то, что видит пользователь в самом чате.
        assert normalize_for_content_match("по-русски") == "порусски"

    def test_space_separated_chunks_not_collapsed(self) -> None:
        # Пробел НЕ получает ослабленное правило (в отличие от дефиса выше)
        # — иначе обычное предложение из нескольких слов схлопнулось бы в
        # одну строку без пробелов целиком.
        assert normalize_for_content_match("пи дор") == "пи дор"

    def test_standalone_number_untouched(self) -> None:
        assert normalize_for_content_match("+100 к скорости") == "+100 к скорости"

    def test_number_attached_to_word_untouched(self) -> None:
        # Цифра сразу после слова без обфускации намерения — не трогаем
        # число само по себе, только leetspeak-символы внутри буквенного
        # прогона (см. _WORD_WITH_LEET_RE в wordlist.py).
        result = normalize_for_content_match("уровень 3 достигнут")
        assert "3" in result

    def test_word_boundary_preserved_after_collapse(self) -> None:
        # Схлопывание одного обфусцированного слова не должно склеивать его
        # с соседним обычным словом через пробел.
        assert normalize_for_content_match("п-р-и-в-е-т мир") == "привет мир"

    def test_empty_string(self) -> None:
        assert normalize_for_content_match("") == ""

    def test_invisible_chars_stripped(self) -> None:
        assert normalize_for_content_match("при​вет") == "привет"
