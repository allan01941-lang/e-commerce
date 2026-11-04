(function () {
    const environmentField = document.getElementById('id_pathao_environment');
    if (!environmentField) {
        return;
    }

    const environmentFields = {
        sandbox: [
            'pathao_sandbox_client_id',
            'pathao_sandbox_client_secret',
            'pathao_sandbox_username',
            'pathao_sandbox_password',
            'pathao_sandbox_base_url',
            'pathao_sandbox_store_id',
        ],
        production: [
            'pathao_production_client_id',
            'pathao_production_client_secret',
            'pathao_production_username',
            'pathao_production_password',
            'pathao_production_base_url',
            'pathao_production_store_id',
        ],
    };

    function updateEnvironmentFields() {
        Object.entries(environmentFields).forEach(function ([environment, fields]) {
            fields.forEach(function (field) {
                const row = document.querySelector('.form-row.field-' + field);
                if (row) {
                    row.style.display = environmentField.value === environment ? '' : 'none';
                }
            });
        });
    }

    environmentField.addEventListener('change', updateEnvironmentFields);
    updateEnvironmentFields();
})();
