const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, OPTIONS",
  "access-control-max-age": "86400",
};

const SQL = `
  SELECT lake_id, lake_name
  FROM lakes
  WHERE lake_name LIKE ? || '%' COLLATE NOCASE
  ORDER BY lake_name
  LIMIT ?
`;

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS });
    }
    if (request.method !== "GET") {
      return Response.json(
        { error: "method not allowed" },
        { status: 405, headers: CORS },
      );
    }

    const url = new URL(request.url);
    const q = url.searchParams.get("q")?.trim();
    const limit = Math.min(
      Number(url.searchParams.get("limit") ?? 50) || 50,
      1000,
    );

    if (!q) {
      return Response.json(
        { error: "missing query parameter 'q'" },
        { status: 400, headers: CORS },
      );
    }

    const { results } = await env.DB.prepare(SQL).bind(q, limit).all();

    return Response.json(
      { query: q, count: results.length, results },
      {
        headers: {
          ...CORS,
          // Cache at the edge — DB rarely changes, results for a given q are stable.
          "cache-control": "public, max-age=300, s-maxage=3600",
        },
      },
    );
  },
};
