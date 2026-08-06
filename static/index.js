/// @t s-check

const button = document.getElementById("submit");
const input = document.getElementById("upload");
const list = document.getElementById("list");

if (!(input instanceof HTMLInputElement)) {
  throw new Error("#upload ist kein Datei-Input");
}

if (!(button instanceof HTMLButtonElement)) {
  throw new Error("#submitButton ist kein Button");
}

function addLi(i, val){
  list.appendChild(document.createElement('p')).append(i, val)
}

!button || button.addEventListener("click", () => {
  console.log("click")
  let files = input.files;
  if (!files) return;
  let filesString = [...files].map(f => f.name).forEach((val, i) =>addLi(i, val));
  document.body.appendChild(document.createElement('p')).append(filesString);
})
