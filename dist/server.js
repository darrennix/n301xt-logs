import express from "express";
import {
  GetObjectCommand,
  HeadObjectCommand,
  NoSuchKey,
  S3Client,
} from "@aws-sdk/client-s3";

const app = express();
const port = Number(process.env.PORT || 8080);

const bucketName = process.env.BUCKET_NAME || process.env.BUCKET || "";
const endpoint = process.env.BUCKET_ENDPOINT || process.env.ENDPOINT || "";
const accessKeyId = process.env.BUCKET_ACCESS_KEY_ID || process.env.ACCESS_KEY_ID || "";
const secretAccessKey = process.env.BUCKET_SECRET_ACCESS_KEY || process.env.SECRET_ACCESS_KEY || "";
const region = process.env.BUCKET_REGION || process.env.REGION || "auto";

if (!bucketName || !endpoint || !accessKeyId || !secretAccessKey) {
  console.warn("Bucket proxy is missing one or more BUCKET_* environment variables.");
}

const s3 = new S3Client({
  region,
  endpoint,
  forcePathStyle: true,
  credentials: {
    accessKeyId,
    secretAccessKey,
  },
});

const contentTypes = {
  ".pdf": "application/pdf",
  ".json": "application/json; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
};

function contentTypeFor(key) {
  const suffix = Object.keys(contentTypes).find((ext) => key.endsWith(ext));
  return suffix ? contentTypes[suffix] : "application/octet-stream";
}

function cleanKey(prefix, rawKey) {
  const key = `${prefix}/${rawKey || ""}`.replaceAll("\\", "/");
  if (!rawKey || key.includes("..") || key.startsWith("/") || key.includes("//")) {
    return null;
  }
  return key;
}

function setAssetHeaders(res, key, metadata = {}) {
  res.setHeader("Content-Type", metadata.ContentType || contentTypeFor(key));
  res.setHeader("Cache-Control", "public, max-age=31536000, immutable");
  res.setHeader("X-Content-Type-Options", "nosniff");
  if (metadata.ContentLength != null) {
    res.setHeader("Content-Length", String(metadata.ContentLength));
  }
  if (metadata.ETag) {
    res.setHeader("ETag", metadata.ETag);
  }
}

function sendMissing(res) {
  res.status(404).type("text/plain").send("Not found");
}

async function proxyBucketObject(req, res, prefix) {
  const key = cleanKey(prefix, req.params[0]);
  if (!key) {
    res.status(400).type("text/plain").send("Invalid asset path");
    return;
  }

  try {
    if (req.method === "HEAD") {
      const object = await s3.send(new HeadObjectCommand({ Bucket: bucketName, Key: key }));
      setAssetHeaders(res, key, object);
      res.status(200).end();
      return;
    }

    const object = await s3.send(new GetObjectCommand({ Bucket: bucketName, Key: key }));
    setAssetHeaders(res, key, object);
    object.Body.pipe(res);
  } catch (error) {
    if (error instanceof NoSuchKey || error?.$metadata?.httpStatusCode === 404) {
      sendMissing(res);
      return;
    }
    console.error(`Failed to proxy ${key}`, error);
    res.status(502).type("text/plain").send("Asset unavailable");
  }
}

app.disable("x-powered-by");
app.use((req, res, next) => {
  res.setHeader("Referrer-Policy", "no-referrer");
  next();
});

app.get("/pages/*", (req, res) => proxyBucketObject(req, res, "pages"));
app.head("/pages/*", (req, res) => proxyBucketObject(req, res, "pages"));
app.get("/text/*", (req, res) => proxyBucketObject(req, res, "text"));
app.head("/text/*", (req, res) => proxyBucketObject(req, res, "text"));

app.use(express.static(".", {
  etag: true,
  maxAge: "1h",
  setHeaders(res) {
    res.setHeader("X-Content-Type-Options", "nosniff");
  },
}));

app.get("*", (_req, res) => {
  res.sendFile("index.html", { root: "." });
});

app.listen(port, "0.0.0.0", () => {
  console.log(`N301XT logbook viewer listening on ${port}`);
});
