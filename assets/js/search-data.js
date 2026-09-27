// get the ninja-keys element
const ninja = document.querySelector('ninja-keys');

// add the home and posts menu items
ninja.data = [{
    id: "nav-home",
    title: "home",
    section: "Navigation",
    handler: () => {
      window.location.href = "/";
    },
  },{id: "nav-bio",
          title: "bio",
          description: "",
          section: "Navigation",
          handler: () => {
            window.location.href = "/bio/";
          },
        },{id: "nav-group",
          title: "group",
          description: "research group",
          section: "Navigation",
          handler: () => {
            window.location.href = "/group/";
          },
        },{id: "nav-publications",
          title: "publications",
          description: "",
          section: "Navigation",
          handler: () => {
            window.location.href = "/publications/";
          },
        },{id: "nav-openings",
          title: "openings",
          description: "",
          section: "Navigation",
          handler: () => {
            window.location.href = "/openings/";
          },
        },{id: "news-i-am-starting-a-new-position-as-lecturer-assistant-professor-at-imperial-college-london",
          title: 'I am starting a new position as Lecturer (Assistant Professor) at Imperial College...',
          description: "",
          section: "News",},{id: "news-we-are-presenting-a-tutorial-on-federated-and-decentralized-multitask-learning-with-roula-nassif-and-ali-h-sayed-at-ieee-icassp-in-singapore",
          title: 'We are presenting a tutorial on “Federated and Decentralized Multitask Learning” with Roula...',
          description: "",
          section: "News",},{id: "news-i-am-speaking-on-decentralised-learning-in-non-convex-environments-at-the-6gic-click-wireless-ai-workshop-hosted-by-the-university-of-surrey-update-a-recording-is-available-on-youtube",
          title: 'I am speaking on “Decentralised Learning in Non-Convex Environments” at the “6GIC-CLICK Wireless...',
          description: "",
          section: "News",},{id: "news-i-am-giving-a-keynote-on-provable-and-efficient-learning-over-networks-at-statos-in-belgrade-serbia",
          title: 'I am giving a keynote on “Provable and Efficient Learning over Networks” at...',
          description: "",
          section: "News",},{id: "news-i-currently-have-an-opening-for-one-fully-funded-international-phd-studentship-please-see-the-advert-for-details-update-this-position-has-been-filled-but-i-continue-to-support-applications-for-departmental-and-college-funding-check-the-openings-tab-if-interested",
          title: 'I currently have an opening for one fully funded international PhD studentship. Please...',
          description: "",
          section: "News",},{id: "news-we-are-organizing-the-3rd-imperial-workshop-on-intelligent-communications-supported-as-part-of-the-ieee-comsoc-excellence-camp-program-on-the-19th-and-20th-of-june-2023-the-workshop-will-feature-keynotes-by-international-speakers-and-poster-sessions-travel-support-is-available-for-student-authors-may-31st-deadline",
          title: 'We are organizing the 3rd Imperial Workshop on Intelligent Communications, supported as part...',
          description: "",
          section: "News",},{
        id: 'social-email',
        title: 'email',
        section: 'Socials',
        handler: () => {
          window.open("mailto:%73.%76%6C%61%73%6B%69@%69%6D%70%65%72%69%61%6C.%61%63.%75%6B", "_blank");
        },
      },{
        id: 'social-linkedin',
        title: 'LinkedIn',
        section: 'Socials',
        handler: () => {
          window.open("https://www.linkedin.com/in/stefanvlaski", "_blank");
        },
      },{
        id: 'social-x',
        title: 'X',
        section: 'Socials',
        handler: () => {
          window.open("https://twitter.com/stefanvlaski", "_blank");
        },
      },{
        id: 'social-github',
        title: 'GitHub',
        section: 'Socials',
        handler: () => {
          window.open("https://github.com/stefanvlaski", "_blank");
        },
      },{
        id: 'social-orcid',
        title: 'ORCID',
        section: 'Socials',
        handler: () => {
          window.open("https://orcid.org/0000-0002-0616-3076", "_blank");
        },
      },{
        id: 'social-rss',
        title: 'RSS Feed',
        section: 'Socials',
        handler: () => {
          window.open("/feed.xml", "_blank");
        },
      },{
        id: 'social-scholar',
        title: 'Google Scholar',
        section: 'Socials',
        handler: () => {
          window.open("https://scholar.google.com/citations?user=mghzVekAAAAJ&hl=en", "_blank");
        },
      },{
      id: 'light-theme',
      title: 'Change theme to light',
      description: 'Change the theme of the site to Light',
      section: 'Theme',
      handler: () => {
        setThemeSetting("light");
      },
    },
    {
      id: 'dark-theme',
      title: 'Change theme to dark',
      description: 'Change the theme of the site to Dark',
      section: 'Theme',
      handler: () => {
        setThemeSetting("dark");
      },
    },
    {
      id: 'system-theme',
      title: 'Use system default theme',
      description: 'Change the theme of the site to System Default',
      section: 'Theme',
      handler: () => {
        setThemeSetting("system");
      },
    },];
