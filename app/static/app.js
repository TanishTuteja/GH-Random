const form = document.querySelector("#repository-form");
const button = document.querySelector("#discover-button");
const result = document.querySelector("#result");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  button.textContent = "Searching the catalog...";
  result.className = "result loading";
  result.textContent = "Finding a match from the active catalog.";

  try {
    const params = new URLSearchParams(new FormData(form));
    const response = await fetch("/api/random-repository?" + params.toString());
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "The catalog could not return a repository.");
    }
    renderRepository(data);
  } catch (error) {
    result.className = "result error";
    result.textContent = error instanceof Error ? error.message : "Something went wrong.";
  } finally {
    button.disabled = false;
    button.innerHTML = 'Discover a repository <span aria-hidden="true">→</span>';
  }
});

function renderRepository(repository) {
  result.replaceChildren();
  result.className = "result repository";

  const label = document.createElement("p");
  label.className = "eyebrow";
  label.textContent = "RANDOM RESULT";

  const title = document.createElement("h2");
  const link = document.createElement("a");
  link.href = repository.html_url;
  link.target = "_blank";
  link.rel = "noreferrer";
  link.textContent = repository.full_name;
  title.append(link);

  const description = document.createElement("p");
  description.className = "description";
  description.textContent = repository.description || "No description was provided.";

  const metadata = document.createElement("dl");
  metadata.className = "metadata";
  addMetadata(metadata, "Language", repository.language || "Not specified");
  addMetadata(metadata, "Stars", repository.stargazer_count.toLocaleString());
  addMetadata(metadata, "Forks", repository.fork_count.toLocaleString());
  addMetadata(metadata, "Open issues", repository.open_issues_count.toLocaleString());
  addMetadata(metadata, "Last pushed", formatDate(repository.github_pushed_at));
  addMetadata(metadata, "Updated", formatDate(repository.github_updated_at));

  const footer = document.createElement("p");
  footer.className = "catalog-note";
  footer.textContent = "Last seen by this catalog: " + formatDate(repository.last_seen_at);

  result.append(label, title, description, metadata);
  if (repository.topics.length) {
    const topics = document.createElement("ul");
    topics.className = "topics";
    repository.topics.forEach((topic) => {
      const item = document.createElement("li");
      item.textContent = topic;
      topics.append(item);
    });
    result.append(topics);
  }
  result.append(footer);
}

function addMetadata(metadata, label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  metadata.append(term, description);
}

function formatDate(value) {
  if (!value) {
    return "Not available";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
