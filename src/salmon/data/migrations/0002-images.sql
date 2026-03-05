CREATE TABLE image_uploads (
    id INTEGER,
    url TEXT NOT NULL,
    deletion_url TEXT,
    time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
);
