-- Phonetic-adaptation heuristics per language pair (Phase 7.16).
--
-- One row tells the respeller how to spot foreign terms of `language_origin`
-- inside text that is read in `language_target` (suffixes, digraphs, tech
-- terms, a native-word stop list, few-shot examples) so a lesson teacher can
-- adapt any pair, not only English terms in Spanish narration.
--
-- The seeds are the Phase 7.15 constants verbatim (tests keep them in sync);
-- rows are editable from the admin GUI at /ui/phonetics.

CREATE TABLE IF NOT EXISTS phonetic_heuristics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language_origin TEXT NOT NULL,
    language_target TEXT NOT NULL,
    language_origin_name TEXT NOT NULL DEFAULT '',
    language_target_name TEXT NOT NULL DEFAULT '',
    suffixes TEXT NOT NULL DEFAULT '[]',
    digraphs TEXT NOT NULL DEFAULT '[]',
    tech_terms TEXT NOT NULL DEFAULT '[]',
    native_words TEXT NOT NULL DEFAULT '[]',
    examples TEXT NOT NULL DEFAULT '{}',
    ascii_only INTEGER NOT NULL DEFAULT 1,
    flag_all_foreign INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (language_origin, language_target)
);

CREATE INDEX IF NOT EXISTS idx_phonetic_heuristics_pair
    ON phonetic_heuristics (language_origin, language_target);

INSERT OR IGNORE INTO phonetic_heuristics (language_origin, language_target, language_origin_name, language_target_name, suffixes, digraphs, tech_terms, native_words, examples, ascii_only, flag_all_foreign) VALUES ('en', 'es', 'English', 'Spanish', '["ing", "tion", "sion", "ment", "ness", "able", "ible", "ical", "ware", "work", "book", "mail", "site", "chat", "bot", "tech"]', '["th", "sh", "ch", "ck", "wh", "ph", "gh", "ou", "oo", "ea"]', '["ai", "amazon", "android", "api", "apple", "benchmark", "blockchain", "chip", "cloud", "cluster", "cpu", "dataset", "deploy", "docker", "firmware", "framework", "funding", "gadget", "gateway", "github", "google", "gpu", "hardware", "ipad", "iphone", "javascript", "kernel", "kubernetes", "learning", "library", "linux", "llm", "machine", "macos", "meta", "microsoft", "nvidia", "openai", "opensource", "patch", "pipeline", "prompt", "python", "release", "runtime", "rust", "sdk", "server", "software", "startup", "streaming", "token", "typescript", "update", "windows", "workflow"]', '["mayor", "lo", "otra", "a", "una", "muy", "nueva", "hace", "de", "desde", "o", "además", "sin", "servicio", "hoy", "sistema", "esa", "para", "sistemas", "tres", "estas", "que", "primer", "nuestro", "tras", "dos", "todo", "estos", "año", "servicios", "donde", "hay", "tecnología", "sí", "tambien", "dia", "gente", "tecnologia", "mañana", "esta", "son", "sobre", "su", "mas", "esto", "empresa", "usuario", "más", "segun", "productos", "nuestra", "sus", "cuando", "no", "les", "empresas", "como", "usuarios", "con", "ademas", "al", "y", "datos", "hasta", "eso", "mejor", "años", "pero", "del", "día", "pais", "mundo", "las", "gran", "en", "país", "si", "entre", "un", "otro", "ya", "también", "por", "este", "días", "puede", "gobierno", "producto", "primera", "según", "los", "se", "vez", "menor", "el", "tiene", "porque", "nuevo", "mientras", "la", "ano", "es", "todos", "cada", "le", "ser", "veces", "fue", "ayer"]', '{"startup": "estartap", "machine learning": "mashin lerning", "streaming": "estriming", "cloud": "claud", "blockchain": "bloqeinin", "Apple": "apel"}', 1, 0);
INSERT OR IGNORE INTO phonetic_heuristics (language_origin, language_target, language_origin_name, language_target_name, suffixes, digraphs, tech_terms, native_words, examples, ascii_only, flag_all_foreign) VALUES ('en', 'fr', 'English', 'French', '["ing", "tion", "sion", "ment", "ness", "able", "ible", "ical", "ware", "work", "book", "mail", "site", "chat", "bot", "tech"]', '["th", "sh", "ch", "ck", "wh", "ph", "gh", "ou", "oo", "ea"]', '["ai", "amazon", "android", "api", "apple", "benchmark", "blockchain", "chip", "cloud", "cluster", "cpu", "dataset", "deploy", "docker", "firmware", "framework", "funding", "gadget", "gateway", "github", "google", "gpu", "hardware", "ipad", "iphone", "javascript", "kernel", "kubernetes", "learning", "library", "linux", "llm", "machine", "macos", "meta", "microsoft", "nvidia", "openai", "opensource", "patch", "pipeline", "prompt", "python", "release", "runtime", "rust", "sdk", "server", "software", "startup", "streaming", "token", "typescript", "update", "windows", "workflow"]', '["produit", "tous", "années", "systèmes", "cette", "des", "les", "mais", "de", "jours", "services", "jour", "ce", "est", "au", "du", "ses", "données", "en", "pas", "un", "comme", "que", "entreprises", "qui", "toute", "avec", "système", "la", "monde", "vous", "pour", "année", "son", "ils", "tout", "elles", "entreprise", "toutes", "le", "plus", "produits", "service", "nous", "sur", "aux", "ces", "une", "sa", "dans", "et"]', '{"startup": "startap", "streaming": "striming", "cloud": "claoud", "machine learning": "machine leurninng"}', 1, 0);
INSERT OR IGNORE INTO phonetic_heuristics (language_origin, language_target, language_origin_name, language_target_name, suffixes, digraphs, tech_terms, native_words, examples, ascii_only, flag_all_foreign) VALUES ('fr', 'es', 'French', 'Spanish', '["eux", "euse", "eaux", "aux", "eau", "ement", "ation", "aison", "ier", "iere", "ille", "ouille", "eille", "oi", "ou", "au", "ai"]', '["eau", "ou", "oi", "ai", "ill", "ph", "th", "gn"]', '[]', '["mayor", "lo", "otra", "a", "una", "muy", "nueva", "hace", "de", "desde", "o", "además", "sin", "servicio", "hoy", "sistema", "esa", "para", "sistemas", "tres", "estas", "que", "primer", "nuestro", "tras", "dos", "todo", "estos", "año", "servicios", "donde", "hay", "tecnología", "sí", "tambien", "dia", "gente", "tecnologia", "mañana", "esta", "son", "sobre", "su", "mas", "esto", "empresa", "usuario", "más", "segun", "productos", "nuestra", "sus", "cuando", "no", "les", "empresas", "como", "usuarios", "con", "ademas", "al", "y", "datos", "hasta", "eso", "mejor", "años", "pero", "del", "día", "pais", "mundo", "las", "gran", "en", "país", "si", "entre", "un", "otro", "ya", "también", "por", "este", "días", "puede", "gobierno", "producto", "primera", "según", "los", "se", "vez", "menor", "el", "tiene", "porque", "nuevo", "mientras", "la", "ano", "es", "todos", "cada", "le", "ser", "veces", "fue", "ayer"]', '{"bonjour": "bonjur"}', 0, 1);
