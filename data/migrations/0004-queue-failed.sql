CREATE TABLE queue_failed (
    url TEXT NOT NULL,
    artist TEXT,
    album TEXT,
    label TEXT,
    time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (url, time)
);
