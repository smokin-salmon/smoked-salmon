CREATE TABLE version (
    id INTEGER,
    PRIMARY KEY (id)
);

CREATE TABLE upload_queue (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    artist TEXT,
    album TEXT,
    label TEXT,
    directory TEXT
);
