(() => {
  const form = document.querySelector(".delivery-method");
  const address = document.querySelector("[data-delivery-address]");
  const input = address?.querySelector("textarea");
  if (!form || !address || !input) return;

  const updateAddressRequirement = () => {
    const selected = form.querySelector('input[name="pickup_method"]:checked');
    const deliverySelected = selected?.value === "delivery";
    address.hidden = !deliverySelected;
    input.required = deliverySelected;
  };

  form.addEventListener("change", updateAddressRequirement);
  updateAddressRequirement();
})();
