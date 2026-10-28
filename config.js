module.exports = {
  platform: 'gitlab',
  logLevel: 'debug',
  labels: ['renovate', 'dependencies', 'automated'],
  onboarding: true,
  onboardingConfig: {
    extends: ['config:recommended']
  },
  cacheDir: "/tmp/renovate",
  recreateClosed: true,
  ignoreDeps: ["python"],
  hostRules: [
    {
      matchHost: "http://172.22.121.51:8081/repository/pypi-public/simple",
      username: "admin",
      password: process.env.RENOVATE_PYPI_PASS
    },
  ],
  enabledManagers: [
    "pep621"
  ],
  "ignoreDeps": ["ray"],
  packageRules: [
    {
      matchManagers: ["pep621"]
    }
  ],
  lockFileMaintenance: {
  enabled: true,
  schedule: [
      "before 6pm every day"
    ]
  }
};
